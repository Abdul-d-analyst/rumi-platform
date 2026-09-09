/**
 * Opt-in deployment telemetry.
 *
 * A deployment that has said yes posts three counts once a day, so the people
 * maintaining the open-source project can see whether it is actually being
 * used. Off unless the operator opted in during `rumi setup` — both
 * RUMI_TELEMETRY=on and a RUMI_DEPLOYMENT_ID must be present, so an opted-in
 * flag on its own (a copied .env, a hand-edited file) never sends anything.
 *
 * What goes out: a random per-deployment id, the version, the resolved drivers,
 * the names of the features that are switched on, and three integers. Nothing
 * about an individual — no phone numbers, no names, no message content, no
 * scores. `buildPayload` is the whole contract; there is no other sender.
 *
 * Never throws. Every function returns a benign value on any failure, because
 * a stats push must never be able to interrupt a teacher's conversation.
 * Modelled on version-check.js, which does the same for the release check.
 */

const DEFAULT_TELEMETRY_URL = 'https://yyhipfppydvcfghqvyjj.supabase.co/functions/v1/telemetry';

/** How long to wait on the receiver before giving up on this run. */
const REQUEST_TIMEOUT_MS = 5000;

/** Windows the two rate-shaped counts are measured over. */
const ACTIVE_WINDOW_DAYS = 7;
const VOLUME_WINDOW_DAYS = 30;

/**
 * True only when the operator opted in AND a deployment id exists.
 *
 * @param {object} env  typically process.env
 * @returns {boolean}
 */
function isTelemetryEnabled(env = {}) {
  return String(env.RUMI_TELEMETRY || '').trim().toLowerCase() === 'on'
    && Boolean(String(env.RUMI_DEPLOYMENT_ID || '').trim());
}

/** The receiver, overridable so a fork can point at its own. */
function telemetryUrl(env = {}) {
  const override = String(env.RUMI_TELEMETRY_URL || '').trim();
  return override || DEFAULT_TELEMETRY_URL;
}

/** An ISO timestamp `days` ago — the lower bound of a count window. */
function since(days, now = new Date()) {
  return new Date(now.getTime() - days * 24 * 60 * 60 * 1000).toISOString();
}

/**
 * One `count(*)`, or null if the query fails.
 *
 * A null means "we could not read this", which the receiver stores as such —
 * far better than a zero that reads as "nobody used it".
 */
async function countOf(build) {
  try {
    const { count, error } = await build();
    if (error) return null;
    return typeof count === 'number' ? count : null;
  } catch (err) {
    return null;
  }
}

/**
 * Registered teachers, excluding test accounts — the same cohort the Morning
 * Brief reports on, so the two never disagree.
 */
function countTeachers(supabase) {
  return countOf(() => supabase
    .from('users')
    .select('*', { count: 'exact', head: true })
    .eq('registration_completed', true)
    .or('is_test_user.is.null,is_test_user.eq.false'));
}

/**
 * Distinct teachers who sent something in the last week.
 *
 * PostgREST has no count(distinct), so this pulls the ids and de-dupes here —
 * the same approach as database/queries.js. Capped, because on a large
 * deployment this is the one query that could return a lot of rows.
 */
async function countActiveTeachers(supabase, now = new Date()) {
  try {
    const { data, error } = await supabase
      .from('conversations')
      .select('user_id')
      .eq('role', 'user')
      .gte('created_at', since(ACTIVE_WINDOW_DAYS, now))
      .limit(100000);

    if (error || !Array.isArray(data)) return null;
    return new Set(data.map((row) => row.user_id).filter(Boolean)).size;
  } catch (err) {
    return null;
  }
}

/** Lesson plans made in the last 30 days. */
function countLessonPlans(supabase, now = new Date()) {
  return countOf(() => supabase
    .from('lesson_plans')
    .select('*', { count: 'exact', head: true })
    .gte('created_at', since(VOLUME_WINDOW_DAYS, now)));
}

/**
 * The three counts. Runs them together; a failure anywhere becomes a null for
 * that count rather than an exception.
 *
 * @returns {Promise<{teachers: number|null, active_teachers_7d: number|null, lesson_plans_30d: number|null}>}
 */
async function collectStats(supabase, now = new Date()) {
  if (!supabase) return { teachers: null, active_teachers_7d: null, lesson_plans_30d: null };

  const [teachers, active, lessonPlans] = await Promise.all([
    countTeachers(supabase),
    countActiveTeachers(supabase, now),
    countLessonPlans(supabase, now),
  ]);

  return { teachers, active_teachers_7d: active, lesson_plans_30d: lessonPlans };
}

/**
 * The full outbound body — the single place to look to know what leaves a
 * deployment. Feature names and the channel driver come from the
 * feature-availability config so this cannot drift from what is really on.
 */
function buildPayload({ env = {}, version, stats } = {}) {
  const payload = {
    deployment_id: String(env.RUMI_DEPLOYMENT_ID || '').trim(),
    version: version || null,
    channel_driver: null,
    queue_driver: String(env.QUEUE_DRIVER || 'sqs').toLowerCase(),
    features: [],
  };

  try {
    const availability = require('../config/feature-availability');
    payload.channel_driver = availability.resolveChannelDriver(env);
    payload.features = availability.availableFeatures(env);
  } catch (err) {
    // Config unavailable — send the counts without the feature list rather
    // than nothing at all.
  }

  if (stats) payload.stats = stats;
  return payload;
}

/**
 * POSTs one payload. Never throws, never retries — a missed day is not worth a
 * retry ladder, and the next run is 24 hours away.
 *
 * @returns {Promise<{ok: boolean, status?: number, error?: string}>}
 */
async function postTelemetry(route, payload, { fetchImpl, env = {} } = {}) {
  const doFetch = fetchImpl || (typeof fetch === 'function' ? fetch : null);
  if (!doFetch) return { ok: false, error: 'no fetch available' };

  try {
    const response = await doFetch(`${telemetryUrl(env)}/${route}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'User-Agent': 'rumi-platform' },
      body: JSON.stringify(payload),
      signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
    });

    if (!response || !response.ok) {
      return { ok: false, status: response ? response.status : undefined };
    }
    return { ok: true, status: response.status };
  } catch (err) {
    return { ok: false, error: err && err.message ? err.message : 'request failed' };
  }
}

/** Emits a semantic event, and stays silent if the logger is unavailable. */
function emit(event, data) {
  try {
    require('./structured-logger').logEvent(event, data);
  } catch (err) {
    // Logging must never be the thing that breaks telemetry.
  }
}

/**
 * The one-off "this deployment exists" call, fired when setup completes so a
 * deployment is counted even if its bot never boots.
 */
async function sendRegistration({ env = {}, version, fetchImpl } = {}) {
  if (!isTelemetryEnabled(env)) {
    emit('telemetry.register.skipped', { reason: 'not_configured' });
    return { ok: false, skipped: true };
  }

  const result = await postTelemetry('register', buildPayload({ env, version }), { fetchImpl, env });
  if (result.ok) emit('telemetry.register.completed', {});
  else emit('telemetry.register.failed', { status: result.status, error: result.error });
  return result;
}

/** The daily push: read the three counts, post them, log the outcome. */
async function sendDailyStats({ env = {}, version, supabase, fetchImpl, now } = {}) {
  if (!isTelemetryEnabled(env)) {
    emit('telemetry.daily_post.skipped', { reason: 'not_configured' });
    return { ok: false, skipped: true };
  }

  const startedAt = Date.now();
  emit('telemetry.daily_post.started', {});

  const stats = await collectStats(supabase, now || new Date());
  const result = await postTelemetry('stats', buildPayload({ env, version, stats }), { fetchImpl, env });

  if (result.ok) emit('telemetry.daily_post.completed', { durationMs: Date.now() - startedAt });
  else emit('telemetry.daily_post.failed', { status: result.status, error: result.error, durationMs: Date.now() - startedAt });

  return result;
}

module.exports = {
  isTelemetryEnabled,
  telemetryUrl,
  collectStats,
  buildPayload,
  postTelemetry,
  sendRegistration,
  sendDailyStats,
  DEFAULT_TELEMETRY_URL,
  REQUEST_TIMEOUT_MS,
  ACTIVE_WINDOW_DAYS,
  VOLUME_WINDOW_DAYS,
};
