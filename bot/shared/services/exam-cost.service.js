'use strict';
/**
 * exam-cost.service — the Cambridge Cost Compass data layer.
 *
 * A vendor-neutral, itemised "true all-in cost to certificate" comparison
 * across exam boards, plus the next registration deadline per board. Static
 * data + arithmetic — deliberately NOT a generative feature: there is no LLM
 * call anywhere in this file, and no API key gates it (see docs/features/exam-cost.md).
 *
 * Everything here is PURE and side-effect-free apart from reading the two JSON
 * datasets off disk (cached after the first read). No Supabase, no messaging
 * driver, no logger — so this file is testable on its own and safe to require
 * from a cron worker that must boot without the bot's optional native deps.
 *
 * Datasets: bot/shared/data/exam-fees.json + exam-deadlines.json. Both carry a
 * top-level `as_of`. The literal string "FIXTURE" means placeholder data, and
 * every formatted reply then says so out loud — a cost tool that quietly
 * quotes made-up numbers is worse than no tool (see the brief's "wins only on
 * genuine itemisation" risk). Drop in the real files, same schema, and the
 * flagging turns itself off.
 *
 * Fee-shape semantics (`fixed_fees[].per`, and the late surcharge's `per`):
 *   "subject"   → charged once per subject entered, every session
 *   "session"   → charged once per exam session
 *   "candidate" → charged once, ever (registration) — NOT repeated per session
 * That distinction is the whole reason the 2-year total isn't just `total × 2`.
 */

const fs = require('fs');
const path = require('path');

const DATA_DIR = path.join(__dirname, '..', 'data');
const FEES_PATH = path.join(DATA_DIR, 'exam-fees.json');
const DEADLINES_PATH = path.join(DATA_DIR, 'exam-deadlines.json');

/** A parent budgeting "two years out" pays the recurring fees once per exam session. */
const SESSIONS_PER_TWO_YEARS = 2;

/** A deadline this close is called out as urgent rather than merely listed. */
const URGENT_WINDOW_DAYS = 21;

/** The two lead times an opt-in reminder fires on. */
const REMINDER_LEAD_DAYS = [14, 3];

const MS_PER_DAY = 24 * 60 * 60 * 1000;

/** WhatsApp hard-caps a text body far above this; 1500 keeps a reply skimmable on a phone. */
const MAX_REPLY_CHARS = 1500;

// Canonical level labels, longest first so "A Level" never matches inside
// "AS Level" and "O Level" never swallows a longer label.
const LEVELS = ['A Level', 'AS Level', 'AS', 'IGCSE', 'O Level'];

// What a parent might type → the canonical level key used in per_subject_fee.
const LEVEL_ALIASES = {
  'o level': 'O Level',
  olevel: 'O Level',
  'o-level': 'O Level',
  ol: 'O Level',
  igcse: 'IGCSE',
  as: 'AS',
  'as level': 'AS',
  'as-level': 'AS',
  'a level': 'A Level',
  alevel: 'A Level',
  'a-level': 'A Level',
  al: 'A Level',
};

// What a parent might type → a board id in exam-fees.json.
const BOARD_ALIASES = {
  cambridge: 'cambridge',
  caie: 'cambridge',
  cie: 'cambridge',
  'british council': 'cambridge',
  bc: 'cambridge',
  akueb: 'aku-eb',
  'aku-eb': 'aku-eb',
  aku: 'aku-eb',
  'bise-lahore': 'bise-lahore',
  biselahore: 'bise-lahore',
  'bise lahore': 'bise-lahore',
  bise: 'bise-lahore',
  // Deliberately NO bare "lahore" alias: "cost O Level 6 Lahore" must read
  // Lahore as the CITY, not as a board. City and board names overlap in
  // Pakistan; the board always needs its "bise-" prefix.
};

// The 6–8 reply labels, in the languages the bot already answers in. Numbers,
// board names, level names and city names are deliberately NOT translated —
// they are what the parent has to type into a portal.
const LABELS = {
  en: {
    heading: 'Exam cost estimate',
    subjects: 'subjects',
    examFees: 'Exam fees',
    lateEntry: 'Late entry',
    oneOff: 'one-off',
    perSession: 'This session',
    twoYearTotal: '2-year total',
    nextDeadline: 'Next deadline',
    estimate: 'Estimate only — placeholder data, verify with the board',
    urgent: 'CLOSING SOON',
  },
  ur: {
    heading: 'امتحانی اخراجات کا تخمینہ',
    subjects: 'مضامین',
    examFees: 'امتحانی فیس',
    lateEntry: 'لیٹ انٹری',
    oneOff: 'ایک بار',
    perSession: 'اس سیشن کا کل',
    twoYearTotal: '2 سال کا کل',
    nextDeadline: 'اگلی آخری تاریخ',
    estimate: 'صرف تخمینہ — عارضی ڈیٹا، بورڈ سے تصدیق کریں',
    urgent: 'وقت کم ہے',
  },
};

let feesCache = null;
let deadlinesCache = null;

function readJson(file) {
  return JSON.parse(fs.readFileSync(file, 'utf8'));
}

/** The fee dataset. Cached; pass `{ reload: true }` (tests) to re-read from disk. */
function loadFees({ reload = false } = {}) {
  if (reload || !feesCache) feesCache = readJson(FEES_PATH);
  return feesCache;
}

/** The deadline dataset. Cached; pass `{ reload: true }` (tests) to re-read from disk. */
function loadDeadlines({ reload = false } = {}) {
  if (reload || !deadlinesCache) deadlinesCache = readJson(DEADLINES_PATH);
  return deadlinesCache;
}

/** True when the dataset is still the shipped placeholder set. */
function isFixture(dataset) {
  return !dataset || dataset.as_of === 'FIXTURE';
}

/** Every board id the current dataset knows, in dataset order. */
function boardIds() {
  return loadFees().boards.map((b) => b.id);
}

/** "cambridge, AKU-EB" / ["aku"] → ['cambridge','aku-eb']; unknown names dropped. */
function resolveBoardIds(input) {
  const raw = Array.isArray(input) ? input : String(input || '').split(',');
  const known = new Set(boardIds());
  const out = [];
  for (const token of raw) {
    const key = String(token || '').trim().toLowerCase();
    if (!key) continue;
    const id = known.has(key) ? key : BOARD_ALIASES[key];
    if (id && known.has(id) && !out.includes(id)) out.push(id);
  }
  return out;
}

/** "o-level" / "A Level" → the canonical level key, or null. */
function resolveLevel(input) {
  const key = String(input || '').trim().toLowerCase().replace(/\s+/g, ' ');
  if (LEVEL_ALIASES[key]) return LEVEL_ALIASES[key];
  const direct = LEVELS.find((l) => l.toLowerCase() === key);
  return direct || null;
}

/** 250000 → "250,000" (grouping only — the currency symbol is added by the caller). */
function formatAmount(n) {
  return Math.round(Number(n) || 0).toLocaleString('en-US');
}

/** 250000 → "PKR 250,000". */
function formatCurrency(n, currency = 'PKR') {
  return `${currency} ${formatAmount(n)}`;
}

/**
 * Itemised cost per board for one candidate.
 *
 * @param {object} opts
 * @param {string} opts.level      canonical or aliased level ("O Level", "igcse", …)
 * @param {number} opts.subjects   how many subjects the candidate is entering
 * @param {string[]} [opts.boardIds]  boards to compare; defaults to every board
 * @param {boolean} [opts.includeLate=false] add the late-entry surcharge
 * @param {string} [opts.city]     city the candidate would sit in (availability flag only)
 * @returns {{level:string, subjects:number, currency:string, asOf:string, isFixture:boolean,
 *           city:(string|null), boards:object[], errors:string[]}}
 */
function estimate({ level, subjects, boardIds: wanted, includeLate = false, city = null } = {}) {
  const fees = loadFees();
  const resolvedLevel = resolveLevel(level);
  const count = Math.trunc(Number(subjects));
  const errors = [];

  if (!resolvedLevel) errors.push(`unknown_level:${level}`);
  if (!Number.isFinite(count) || count < 1) errors.push(`invalid_subjects:${subjects}`);

  const ids = wanted && wanted.length ? resolveBoardIds(wanted) : boardIds();
  const boards = [];

  if (!errors.length) {
    for (const id of ids) {
      const board = fees.boards.find((b) => b.id === id);
      if (!board) continue;

      const perSubject = (board.per_subject_fee || {})[resolvedLevel];
      if (typeof perSubject !== 'number') {
        boards.push({
          id: board.id,
          name: board.name,
          supported: false,
          reason: 'level_not_offered',
          levels: board.levels || [],
          items: [],
          sessionTotal: 0,
          oneOffTotal: 0,
          total: 0,
          twoYearTotal: 0,
          cityAvailable: null,
          notes: board.notes || '',
          source: board.source || null,
        });
        continue;
      }

      const items = [{
        label: `${resolvedLevel} × ${count}`,
        amount: perSubject * count,
        per: 'subject',
        unitAmount: perSubject,
      }];

      for (const fee of board.fixed_fees || []) {
        const multiplier = fee.per === 'subject' ? count : 1;
        items.push({
          label: fee.label,
          amount: (Number(fee.amount) || 0) * multiplier,
          per: fee.per,
          unitAmount: Number(fee.amount) || 0,
        });
      }

      const late = board.late_entry_surcharge;
      if (includeLate && late && typeof late.amount === 'number') {
        items.push({
          label: `${late.stage} surcharge`,
          amount: late.amount * (late.per === 'subject' ? count : 1),
          per: late.per,
          unitAmount: late.amount,
        });
      }

      // "candidate" fees are charged once, ever; everything else recurs each session.
      const oneOffTotal = items.filter((i) => i.per === 'candidate')
        .reduce((s, i) => s + i.amount, 0);
      const sessionTotal = items.filter((i) => i.per !== 'candidate')
        .reduce((s, i) => s + i.amount, 0);

      boards.push({
        id: board.id,
        name: board.name,
        supported: true,
        items,
        sessionTotal,
        oneOffTotal,
        total: sessionTotal + oneOffTotal,
        twoYearTotal: sessionTotal * SESSIONS_PER_TWO_YEARS + oneOffTotal,
        cityAvailable: city
          ? (board.cities || []).some((c) => c.toLowerCase() === String(city).toLowerCase())
          : null,
        privateCandidateAllowed: board.private_candidate_allowed !== false,
        notes: board.notes || '',
        source: board.source || null,
      });
    }

    if (!boards.length) errors.push('no_known_boards');
  }

  return {
    level: resolvedLevel,
    subjects: Number.isFinite(count) ? count : null,
    currency: fees.currency || 'PKR',
    asOf: fees.as_of,
    isFixture: isFixture(fees),
    includeLate: !!includeLate,
    city: city || null,
    sessionsPerTwoYears: SESSIONS_PER_TWO_YEARS,
    boards,
    errors,
  };
}

/**
 * Upcoming deadlines for a board, soonest first, each with `daysUntil` and an
 * `urgent` flag (≤ 21 days out).
 *
 * The window is decided at CALL time from `now`, never baked into a schedule —
 * the same rule bot/workers/brief.worker.js follows for "which brief is today".
 * A cron that fires late, twice, or in the wrong timezone therefore cannot send
 * a stale "deadline is coming" for a date that has already passed.
 *
 * @param {string|null} boardId  a board id/alias, or null/'all' for every board
 * @param {Date|string} [now]
 */
function nextDeadlines(boardId, now = new Date()) {
  const data = loadDeadlines();
  const today = now instanceof Date ? now : new Date(now);
  const startOfToday = Date.UTC(today.getUTCFullYear(), today.getUTCMonth(), today.getUTCDate());

  const wanted = !boardId || boardId === 'all' ? null : resolveBoardIds(boardId)[0] || boardId;

  return (data.deadlines || [])
    .filter((d) => (wanted ? d.board === wanted : true))
    .map((d) => {
      const at = new Date(`${d.date}T00:00:00Z`).getTime();
      const daysUntil = Math.round((at - startOfToday) / MS_PER_DAY);
      return { ...d, daysUntil, urgent: daysUntil >= 0 && daysUntil <= URGENT_WINDOW_DAYS };
    })
    .filter((d) => d.daysUntil >= 0)
    .sort((a, b) => a.daysUntil - b.daysUntil);
}

/**
 * Deadlines exactly `leadDays` away — the reminder trigger. Same "decide at run
 * time" rule as nextDeadlines: a run computes the day count from `now` rather
 * than trusting that it fired on the day it was scheduled for.
 */
function deadlinesDueForReminder(boardId, now = new Date(), leadDays = REMINDER_LEAD_DAYS) {
  return nextDeadlines(boardId, now).filter((d) => leadDays.includes(d.daysUntil));
}

function labelsFor(language) {
  const key = String(language || 'en').slice(0, 2).toLowerCase();
  return LABELS[key] || LABELS.en;
}

/** Trim to MAX_REPLY_CHARS on a line boundary so a block never ends mid-number. */
function clampReply(text) {
  if (text.length <= MAX_REPLY_CHARS) return text;
  const lines = text.split('\n');
  const kept = [];
  let used = 0;
  for (const line of lines) {
    if (used + line.length + 1 > MAX_REPLY_CHARS - 2) break;
    kept.push(line);
    used += line.length + 1;
  }
  return `${kept.join('\n')}\n…`;
}

/** Usage help — also the reply when a natural-language "exam fee" question arrives. */
function formatUsage(language = 'en') {
  const known = loadFees().boards.map((b) => b.id).join(', ');
  const L = labelsFor(language);
  return [
    `📊 ${L.heading}`,
    '',
    'Send:',
    '• cost "O Level" 6 cambridge,aku-eb Karachi',
    '• deadlines cambridge',
    '• remind me cambridge   (stop reminders)',
    '',
    `Boards: ${known}`,
    `Levels: ${LEVELS.filter((l) => l !== 'AS Level').join(', ')}`,
  ].join('\n');
}

/** The WhatsApp-friendly cost reply: one block per board, plain text, ≤ 1500 chars. */
function formatEstimateReply(result, language = 'en') {
  const L = labelsFor(language);
  if (result.errors && result.errors.length) return formatUsage(language);

  const cur = result.currency;
  const out = [
    `📊 ${L.heading} — ${result.level}, ${result.subjects} ${L.subjects}${result.city ? ` · ${result.city}` : ''}`,
  ];

  const supported = result.boards.filter((b) => b.supported);
  const unsupported = result.boards.filter((b) => !b.supported);
  const ranked = [...supported].sort((a, b) => a.twoYearTotal - b.twoYearTotal);

  for (const board of ranked) {
    out.push('');
    out.push(`*${board.name}*`);
    for (const item of board.items) {
      const suffix = item.per === 'candidate' ? ` (${L.oneOff})` : '';
      out.push(`• ${item.label}: ${formatCurrency(item.amount, cur)}${suffix}`);
    }
    out.push(`${L.perSession}: ${formatCurrency(board.total, cur)}`);
    out.push(`${L.twoYearTotal}: ${formatCurrency(board.twoYearTotal, cur)}`);
    if (board.cityAvailable === false) {
      out.push(`⚠️ No listed centre in ${result.city} (${board.name})`);
    }
  }

  for (const board of unsupported) {
    out.push('');
    out.push(`*${board.name}* — no ${result.level} listed (offers: ${board.levels.join(', ')})`);
  }

  out.push('');
  out.push(result.isFixture
    ? `⚠️ ${L.estimate} (as_of: ${result.asOf})`
    : `ℹ️ ${L.estimate.split('—')[0].trim()} · as_of ${result.asOf}`);

  return clampReply(out.join('\n'));
}

/** The WhatsApp-friendly deadline reply. */
function formatDeadlinesReply(deadlines, language = 'en', { asOf = loadDeadlines().as_of } = {}) {
  const L = labelsFor(language);
  if (!deadlines.length) {
    return `${L.nextDeadline}: —\nNo upcoming dates in the dataset (as_of: ${asOf}).`;
  }

  const out = [`🗓️ ${L.nextDeadline}`];
  for (const d of deadlines) {
    const flag = d.urgent ? ` ⚠️ ${L.urgent}` : '';
    const impact = typeof d.fee_impact === 'number'
      ? ` · +${formatCurrency(d.fee_impact)}/subject`
      : '';
    out.push('');
    out.push(`*${d.board}* — ${d.session} (${d.stage})`);
    out.push(`${d.date} · ${d.daysUntil} days${flag}${impact}`);
    out.push(`confidence: ${d.confidence}`);
  }
  out.push('');
  out.push(asOf === 'FIXTURE'
    ? `⚠️ ${L.estimate} (as_of: ${asOf})`
    : `as_of ${asOf}`);

  return clampReply(out.join('\n'));
}

/** One reminder message for one deadline. */
function formatReminderMessage(deadline, language = 'en') {
  const L = labelsFor(language);
  const impact = typeof deadline.fee_impact === 'number'
    ? `\nMiss it and the ${deadline.stage} fee adds ${formatCurrency(deadline.fee_impact)}/subject.`
    : '';
  return clampReply([
    `🗓️ ${L.nextDeadline}: ${deadline.board} — ${deadline.session} (${deadline.stage})`,
    `${deadline.date} · in ${deadline.daysUntil} days.${impact}`,
    '',
    'Reply "stop reminders" any time.',
  ].join('\n'));
}

module.exports = {
  // constants worth asserting on / reusing
  SESSIONS_PER_TWO_YEARS,
  URGENT_WINDOW_DAYS,
  REMINDER_LEAD_DAYS,
  MAX_REPLY_CHARS,
  LEVELS,
  LABELS,
  // data
  loadFees,
  loadDeadlines,
  isFixture,
  boardIds,
  resolveBoardIds,
  resolveLevel,
  // maths
  estimate,
  nextDeadlines,
  deadlinesDueForReminder,
  // formatting
  formatAmount,
  formatCurrency,
  formatUsage,
  formatEstimateReply,
  formatDeadlinesReply,
  formatReminderMessage,
};
