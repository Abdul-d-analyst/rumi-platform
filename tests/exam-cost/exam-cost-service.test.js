/**
 * Cost Compass maths — fixture-driven.
 *
 * Every expectation is derived from bot/shared/data/exam-fees.json rather than
 * hardcoded, so dropping in the real dataset does not turn this suite red for
 * the wrong reason. The one hardcoded case is the `per` semantics arithmetic,
 * which IS the contract worth pinning.
 */

const ExamCostService = require('../../bot/shared/services/exam-cost.service');

const fees = ExamCostService.loadFees({ reload: true });
const cambridge = fees.boards.find((b) => b.id === 'cambridge');

describe('exam-cost service — datasets', () => {
  it('ships both datasets marked as fixtures', () => {
    expect(fees.as_of).toBe('FIXTURE');
    expect(ExamCostService.isFixture(fees)).toBe(true);
    expect(ExamCostService.isFixture(ExamCostService.loadDeadlines({ reload: true }))).toBe(true);
  });

  it('carries three boards, each with a source URL and a level list', () => {
    expect(fees.boards).toHaveLength(3);
    for (const board of fees.boards) {
      expect(board.source).toMatch(/^https?:\/\//);
      expect(board.levels.length).toBeGreaterThan(0);
      expect(Object.keys(board.per_subject_fee).length).toBeGreaterThan(0);
    }
  });

  it('every deadline names a board that exists in the fee dataset', () => {
    const ids = new Set(ExamCostService.boardIds());
    for (const d of ExamCostService.loadDeadlines().deadlines) {
      expect(ids.has(d.board)).toBe(true);
      expect(d.confidence).toMatch(/^(confirmed|estimated)$/);
    }
  });
});

describe('exam-cost service — resolvers', () => {
  it('resolves level spellings to the canonical key', () => {
    expect(ExamCostService.resolveLevel('o-level')).toBe('O Level');
    expect(ExamCostService.resolveLevel('O LEVEL')).toBe('O Level');
    expect(ExamCostService.resolveLevel('igcse')).toBe('IGCSE');
    expect(ExamCostService.resolveLevel('a level')).toBe('A Level');
    expect(ExamCostService.resolveLevel('nonsense')).toBeNull();
  });

  it('resolves board aliases and drops unknown names', () => {
    expect(ExamCostService.resolveBoardIds('cambridge, akueb')).toEqual(['cambridge', 'aku-eb']);
    expect(ExamCostService.resolveBoardIds(['CAIE'])).toEqual(['cambridge']);
    expect(ExamCostService.resolveBoardIds('hogwarts')).toEqual([]);
  });

  it('never reads a bare city name as a board (Lahore is a city here)', () => {
    expect(ExamCostService.resolveBoardIds('Lahore')).toEqual([]);
    expect(ExamCostService.resolveBoardIds('bise-lahore')).toEqual(['bise-lahore']);
  });

  it('de-duplicates repeated boards', () => {
    expect(ExamCostService.resolveBoardIds('cambridge,cie,caie')).toEqual(['cambridge']);
  });
});

describe('exam-cost service — estimate()', () => {
  it('itemises per-subject fees against the dataset', () => {
    const result = ExamCostService.estimate({ level: 'O Level', subjects: 6, boardIds: ['cambridge'] });
    const board = result.boards[0];
    const subjectLine = board.items[0];

    expect(subjectLine.amount).toBe(cambridge.per_subject_fee['O Level'] * 6);
    expect(subjectLine.per).toBe('subject');
    expect(board.items).toHaveLength(1 + cambridge.fixed_fees.length);
  });

  it('splits one-off "candidate" fees from recurring "session" fees', () => {
    const result = ExamCostService.estimate({ level: 'O Level', subjects: 6, boardIds: ['cambridge'] });
    const board = result.boards[0];

    const expectedOneOff = cambridge.fixed_fees
      .filter((f) => f.per === 'candidate')
      .reduce((s, f) => s + f.amount, 0);
    const expectedSession = cambridge.per_subject_fee['O Level'] * 6
      + cambridge.fixed_fees.filter((f) => f.per === 'session').reduce((s, f) => s + f.amount, 0);

    expect(board.oneOffTotal).toBe(expectedOneOff);
    expect(board.sessionTotal).toBe(expectedSession);
    expect(board.total).toBe(expectedSession + expectedOneOff);
  });

  it('charges recurring fees twice and one-off fees once in the 2-year total', () => {
    const result = ExamCostService.estimate({ level: 'O Level', subjects: 6, boardIds: ['cambridge'] });
    const board = result.boards[0];

    expect(ExamCostService.SESSIONS_PER_TWO_YEARS).toBe(2);
    expect(board.twoYearTotal).toBe(board.sessionTotal * 2 + board.oneOffTotal);
    // The distinction is load-bearing: a naive total × 2 would over-count.
    expect(board.twoYearTotal).toBeLessThan(board.total * 2);
  });

  it('adds the late-entry surcharge only when asked, priced per subject', () => {
    const plain = ExamCostService.estimate({ level: 'O Level', subjects: 4, boardIds: ['cambridge'] });
    const late = ExamCostService.estimate({
      level: 'O Level', subjects: 4, boardIds: ['cambridge'], includeLate: true,
    });
    const surcharge = cambridge.late_entry_surcharge;

    expect(surcharge.per).toBe('subject');
    expect(late.boards[0].total - plain.boards[0].total).toBe(surcharge.amount * 4);
    expect(late.includeLate).toBe(true);
  });

  it('compares every board when none is named', () => {
    const result = ExamCostService.estimate({ level: 'O Level', subjects: 3 });
    expect(result.boards.map((b) => b.id)).toEqual(ExamCostService.boardIds());
  });

  it('marks a board that does not offer the level as unsupported rather than costing it', () => {
    const result = ExamCostService.estimate({ level: 'IGCSE', subjects: 5 });
    const akueb = result.boards.find((b) => b.id === 'aku-eb');

    expect(akueb.supported).toBe(false);
    expect(akueb.reason).toBe('level_not_offered');
    expect(akueb.total).toBe(0);
  });

  it('flags city availability without changing the cost', () => {
    const karachi = ExamCostService.estimate({
      level: 'O Level', subjects: 2, boardIds: ['aku-eb', 'bise-lahore'], city: 'Karachi',
    });
    expect(karachi.boards.find((b) => b.id === 'aku-eb').cityAvailable).toBe(true);
    expect(karachi.boards.find((b) => b.id === 'bise-lahore').cityAvailable).toBe(false);

    const noCity = ExamCostService.estimate({ level: 'O Level', subjects: 2, boardIds: ['aku-eb'] });
    expect(noCity.boards[0].cityAvailable).toBeNull();
    expect(noCity.boards[0].total).toBe(karachi.boards.find((b) => b.id === 'aku-eb').total);
  });

  it('reports errors instead of throwing on bad input', () => {
    expect(ExamCostService.estimate({ level: 'B Level', subjects: 3 }).errors)
      .toContain('unknown_level:B Level');
    expect(ExamCostService.estimate({ level: 'O Level', subjects: 0 }).errors)
      .toContain('invalid_subjects:0');
    expect(ExamCostService.estimate({ level: 'O Level', subjects: 'six' }).errors[0])
      .toMatch(/^invalid_subjects/);
    expect(ExamCostService.estimate({}).errors.length).toBeGreaterThan(0);
  });

  it('propagates the fixture flag so callers can label the reply', () => {
    const result = ExamCostService.estimate({ level: 'O Level', subjects: 1 });
    expect(result.isFixture).toBe(true);
    expect(result.asOf).toBe('FIXTURE');
    expect(result.currency).toBe('PKR');
  });
});

describe('exam-cost service — nextDeadlines() urgency window', () => {
  // The fixture's soonest Cambridge deadline.
  const NORMAL = '2027-02-12';

  it('sorts soonest-first and drops anything already past', () => {
    const list = ExamCostService.nextDeadlines('cambridge', new Date('2027-03-01T00:00:00Z'));
    expect(list.every((d) => d.daysUntil >= 0)).toBe(true);
    expect(list.map((d) => d.date)).not.toContain(NORMAL);
    const days = list.map((d) => d.daysUntil);
    expect([...days].sort((a, b) => a - b)).toEqual(days);
  });

  it('flags urgent at exactly the 21-day boundary and not one day beyond', () => {
    expect(ExamCostService.URGENT_WINDOW_DAYS).toBe(21);

    const at21 = ExamCostService.nextDeadlines('cambridge', new Date('2027-01-22T00:00:00Z'))[0];
    expect(at21.date).toBe(NORMAL);
    expect(at21.daysUntil).toBe(21);
    expect(at21.urgent).toBe(true);

    const at22 = ExamCostService.nextDeadlines('cambridge', new Date('2027-01-21T00:00:00Z'))[0];
    expect(at22.daysUntil).toBe(22);
    expect(at22.urgent).toBe(false);
  });

  it('keeps a deadline falling today (daysUntil 0) and marks it urgent', () => {
    const today = ExamCostService.nextDeadlines('cambridge', new Date('2027-02-12T18:45:00Z'))[0];
    expect(today.date).toBe(NORMAL);
    expect(today.daysUntil).toBe(0);
    expect(today.urgent).toBe(true);
  });

  it('decides the window from `now`, so the same data reads differently on two days', () => {
    const early = ExamCostService.nextDeadlines('cambridge', new Date('2026-12-01T00:00:00Z'))[0];
    const late = ExamCostService.nextDeadlines('cambridge', new Date('2027-02-05T00:00:00Z'))[0];
    expect(early.urgent).toBe(false);
    expect(late.urgent).toBe(true);
    expect(early.date).toBe(late.date);
  });

  it('returns every board when no board is named', () => {
    const all = ExamCostService.nextDeadlines(null, new Date('2026-01-01T00:00:00Z'));
    expect(new Set(all.map((d) => d.board)).size).toBeGreaterThan(1);
    expect(ExamCostService.nextDeadlines('all', new Date('2026-01-01T00:00:00Z'))).toHaveLength(all.length);
  });

  it('accepts a board alias', () => {
    const viaAlias = ExamCostService.nextDeadlines('akueb', new Date('2026-01-01T00:00:00Z'));
    expect(viaAlias.length).toBeGreaterThan(0);
    expect(viaAlias.every((d) => d.board === 'aku-eb')).toBe(true);
  });
});

describe('exam-cost service — deadlinesDueForReminder()', () => {
  it('fires on exactly 14 and 3 days out, and on nothing in between', () => {
    expect(ExamCostService.REMINDER_LEAD_DAYS).toEqual([14, 3]);

    const at14 = ExamCostService.deadlinesDueForReminder('cambridge', new Date('2027-01-29T00:00:00Z'));
    expect(at14.map((d) => d.date)).toEqual(['2027-02-12']);

    const at3 = ExamCostService.deadlinesDueForReminder('cambridge', new Date('2027-02-09T00:00:00Z'));
    expect(at3.map((d) => d.date)).toEqual(['2027-02-12']);

    expect(ExamCostService.deadlinesDueForReminder('cambridge', new Date('2027-02-04T00:00:00Z')))
      .toEqual([]);
  });

  it('does not fire the day after the deadline passed', () => {
    expect(ExamCostService.deadlinesDueForReminder('cambridge', new Date('2027-02-13T00:00:00Z'))
      .some((d) => d.date === '2027-02-12')).toBe(false);
  });
});
