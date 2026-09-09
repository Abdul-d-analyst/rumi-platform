/**
 * Opt-in deployment telemetry — the sender.
 *
 * Two things matter here and everything else is detail: it must stay silent
 * unless the operator opted in, and it must never throw whatever the network
 * does. Modelled on version-check.test.js, with fetch injected rather than
 * patched onto the global.
 */

const telemetry = require('../../bot/shared/utils/telemetry');

const DEPLOYMENT_ID = '3f2a7c18-6b4e-4d51-9a0c-2e8f5b1d7c44';
const OPTED_IN = { RUMI_TELEMETRY: 'on', RUMI_DEPLOYMENT_ID: DEPLOYMENT_ID };

/** A fetch double that records calls and answers with `response`. */
function fakeFetch(response = { ok: true, status: 202 }) {
  return jest.fn().mockResolvedValue(response);
}

/** A Supabase double whose chain resolves to whatever the caller wants. */
function fakeSupabase({ count = 0, rows = [], error = null } = {}) {
  const chain = {
    select: jest.fn(() => chain),
    eq: jest.fn(() => chain),
    or: jest.fn(() => chain),
    gte: jest.fn(() => chain),
    limit: jest.fn(() => Promise.resolve({ data: rows, error })),
    then: (resolve) => resolve({ count, data: rows, error }),
  };
  return { from: jest.fn(() => chain) };
}

describe('isTelemetryEnabled', () => {
  test('is off when nothing is configured', () => {
    expect(telemetry.isTelemetryEnabled({})).toBe(false);
  });

  test('is off when the flag is on but no deployment id was ever generated', () => {
    expect(telemetry.isTelemetryEnabled({ RUMI_TELEMETRY: 'on' })).toBe(false);
  });

  test('is off when an id exists but the operator said no', () => {
    expect(telemetry.isTelemetryEnabled({ RUMI_TELEMETRY: 'off', RUMI_DEPLOYMENT_ID: DEPLOYMENT_ID })).toBe(false);
  });

  test('is on only when both the flag and an id are present', () => {
    expect(telemetry.isTelemetryEnabled(OPTED_IN)).toBe(true);
  });

  test('tolerates casing and stray whitespace in the flag', () => {
    expect(telemetry.isTelemetryEnabled({ ...OPTED_IN, RUMI_TELEMETRY: ' ON ' })).toBe(true);
  });
});

describe('telemetryUrl', () => {
  test('defaults to the project endpoint', () => {
    expect(telemetry.telemetryUrl({})).toBe(telemetry.DEFAULT_TELEMETRY_URL);
  });

  test('a fork can point at its own receiver', () => {
    expect(telemetry.telemetryUrl({ RUMI_TELEMETRY_URL: 'https://example.test/hook' })).toBe('https://example.test/hook');
  });
});

describe('buildPayload', () => {
  test('carries the deployment id, version and drivers', () => {
    const payload = telemetry.buildPayload({ env: { ...OPTED_IN, QUEUE_DRIVER: 'bullmq' }, version: '2.9.39' });

    expect(payload.deployment_id).toBe(DEPLOYMENT_ID);
    expect(payload.version).toBe('2.9.39');
    expect(payload.queue_driver).toBe('bullmq');
    expect(typeof payload.channel_driver).toBe('string');
    expect(Array.isArray(payload.features)).toBe(true);
  });

  test('omits stats on a registration payload', () => {
    expect(telemetry.buildPayload({ env: OPTED_IN, version: '1.0.0' }).stats).toBeUndefined();
  });

  test('carries exactly the three counts and nothing else', () => {
    const stats = { teachers: 340, active_teachers_7d: 210, lesson_plans_30d: 1180 };
    const payload = telemetry.buildPayload({ env: OPTED_IN, version: '1.0.0', stats });

    expect(Object.keys(payload.stats).sort()).toEqual(['active_teachers_7d', 'lesson_plans_30d', 'teachers']);
  });

  test('never carries anything identifying a person', () => {
    const stats = { teachers: 1, active_teachers_7d: 1, lesson_plans_30d: 1 };
    const serialised = JSON.stringify(telemetry.buildPayload({ env: OPTED_IN, version: '1.0.0', stats }));

    for (const forbidden of ['phone_number', 'first_name', 'last_name', 'school_name', 'name']) {
      expect(serialised).not.toContain(forbidden);
    }
  });
});

describe('postTelemetry', () => {
  test('posts JSON to the route and identifies itself', async () => {
    const doFetch = fakeFetch();
    await telemetry.postTelemetry('stats', { deployment_id: DEPLOYMENT_ID }, { fetchImpl: doFetch, env: {} });

    const [url, options] = doFetch.mock.calls[0];
    expect(url).toBe(`${telemetry.DEFAULT_TELEMETRY_URL}/stats`);
    expect(options.method).toBe('POST');
    expect(options.headers).toEqual(expect.objectContaining({
      'Content-Type': 'application/json',
      'User-Agent': 'rumi-platform',
    }));
    expect(JSON.parse(options.body).deployment_id).toBe(DEPLOYMENT_ID);
  });

  test('gives up rather than hanging forever', async () => {
    const doFetch = fakeFetch();
    await telemetry.postTelemetry('stats', {}, { fetchImpl: doFetch, env: {} });

    expect(doFetch.mock.calls[0][1].signal).toBeDefined();
  });

  test('returns not-ok instead of throwing when the receiver rejects', async () => {
    const result = await telemetry.postTelemetry('stats', {}, {
      fetchImpl: jest.fn().mockResolvedValue({ ok: false, status: 400 }),
      env: {},
    });

    expect(result).toEqual({ ok: false, status: 400 });
  });

  test('returns not-ok instead of throwing when the network fails', async () => {
    const result = await telemetry.postTelemetry('stats', {}, {
      fetchImpl: jest.fn().mockRejectedValue(new Error('ECONNREFUSED')),
      env: {},
    });

    expect(result.ok).toBe(false);
    expect(result.error).toBe('ECONNREFUSED');
  });
});

describe('collectStats', () => {
  test('returns nulls rather than zeros when there is no database', async () => {
    expect(await telemetry.collectStats(null)).toEqual({
      teachers: null, active_teachers_7d: null, lesson_plans_30d: null,
    });
  });

  test('counts distinct active teachers, not messages', async () => {
    const supabase = fakeSupabase({ count: 12, rows: [{ user_id: 'a' }, { user_id: 'a' }, { user_id: 'b' }] });
    const stats = await telemetry.collectStats(supabase);

    expect(stats.active_teachers_7d).toBe(2);
  });

  test('excludes test accounts and unregistered users from the teacher count', async () => {
    const supabase = fakeSupabase({ count: 5 });
    await telemetry.collectStats(supabase);

    const chain = supabase.from.mock.results[0].value;
    expect(chain.eq).toHaveBeenCalledWith('registration_completed', true);
    expect(chain.or).toHaveBeenCalledWith('is_test_user.is.null,is_test_user.eq.false');
  });

  test('a failing query becomes a null, not an exception', async () => {
    const supabase = fakeSupabase({ error: { message: 'permission denied' } });
    const stats = await telemetry.collectStats(supabase);

    expect(stats).toEqual({ teachers: null, active_teachers_7d: null, lesson_plans_30d: null });
  });

  test('survives a database client that throws outright', async () => {
    const supabase = { from: () => { throw new Error('client exploded'); } };

    await expect(telemetry.collectStats(supabase)).resolves.toEqual({
      teachers: null, active_teachers_7d: null, lesson_plans_30d: null,
    });
  });
});

describe('sendDailyStats', () => {
  test('sends nothing when the operator has not opted in', async () => {
    const doFetch = fakeFetch();
    const result = await telemetry.sendDailyStats({ env: {}, version: '1.0.0', supabase: fakeSupabase(), fetchImpl: doFetch });

    expect(doFetch).not.toHaveBeenCalled();
    expect(result.skipped).toBe(true);
  });

  test('posts the counts to the stats route once opted in', async () => {
    const doFetch = fakeFetch();
    const result = await telemetry.sendDailyStats({
      env: OPTED_IN, version: '2.9.39', supabase: fakeSupabase({ count: 7, rows: [{ user_id: 'a' }] }), fetchImpl: doFetch,
    });

    expect(result.ok).toBe(true);
    const [url, options] = doFetch.mock.calls[0];
    expect(url.endsWith('/stats')).toBe(true);
    expect(JSON.parse(options.body).stats).toEqual(expect.objectContaining({ active_teachers_7d: 1 }));
  });

  test('resolves rather than throwing when the receiver is unreachable', async () => {
    const result = await telemetry.sendDailyStats({
      env: OPTED_IN, version: '1.0.0', supabase: fakeSupabase(), fetchImpl: jest.fn().mockRejectedValue(new Error('down')),
    });

    expect(result.ok).toBe(false);
  });
});

describe('sendRegistration', () => {
  test('sends nothing when the operator has not opted in', async () => {
    const doFetch = fakeFetch();
    await telemetry.sendRegistration({ env: {}, version: '1.0.0', fetchImpl: doFetch });

    expect(doFetch).not.toHaveBeenCalled();
  });

  test('posts to the register route with no counts attached', async () => {
    const doFetch = fakeFetch();
    await telemetry.sendRegistration({ env: OPTED_IN, version: '2.9.39', fetchImpl: doFetch });

    const [url, options] = doFetch.mock.calls[0];
    expect(url.endsWith('/register')).toBe(true);
    expect(JSON.parse(options.body).stats).toBeUndefined();
  });
});
