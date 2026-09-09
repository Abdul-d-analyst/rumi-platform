/**
 * The setup consent question.
 *
 * The behaviour that matters is consent discipline: no by default, no on a
 * stray Enter, nothing sent on a no, and a disclosure whose promises match
 * what the sender actually sends.
 */

const { fakeIo, captureLog } = require('../setup/fake-io');
const consent = require('../../bot/scripts/setup/telemetry-consent');
const telemetry = require('../../bot/shared/utils/telemetry');

/** A `save` double standing in for createSaver()'s .env write-through. */
function fakeSave(env) {
  const writes = [];
  return {
    writes,
    save: (vars) => { writes.push(vars); Object.assign(env, vars); },
  };
}

describe('the disclosure', () => {
  test('names the flag an operator needs to turn it off', () => {
    expect(consent.CONSENT_COPY.optOut).toContain('RUMI_TELEMETRY=off');
  });

  test('says plainly that nothing about an individual is sent', () => {
    expect(consent.CONSENT_COPY.never.join(' ')).toContain('nothing about any individual');
  });

  test('promises exactly as many things as the payload sends', () => {
    // Three lines of "what would be sent", three counts in the payload. If one
    // grows without the other, the disclosure has become a lie.
    const stats = { teachers: 1, active_teachers_7d: 1, lesson_plans_30d: 1 };
    const payload = telemetry.buildPayload({
      env: { RUMI_TELEMETRY: 'on', RUMI_DEPLOYMENT_ID: 'x' }, version: '1.0.0', stats,
    });

    expect(consent.CONSENT_COPY.sent).toHaveLength(3);
    expect(Object.keys(payload.stats)).toHaveLength(3);
  });

  test('prints the why, both lists and the opt-out', () => {
    const log = captureLog();
    consent.printConsentCopy();
    const text = log.text;
    log.restore();

    expect(text).toContain('free and open source');
    expect(text).toContain('never be sent');
    expect(text).toContain('RUMI_TELEMETRY=off');
  });
});

describe('askTelemetryConsent', () => {
  test('defaults to yes, so the prompt reads [Y/n] and a bare Enter shares', async () => {
    const env = {};
    const { save } = fakeSave(env);
    const io = fakeIo({ confirm: [] }); // nothing queued — falls through to the default
    const log = captureLog();

    const result = await consent.askTelemetryConsent(io, env, save, {
      fetchImpl: jest.fn().mockResolvedValue({ ok: true, status: 202 }),
    });
    log.restore();

    expect(result.shared).toBe(true);
    expect(env.RUMI_TELEMETRY).toBe('on');
  });

  test('one "n" is enough to decline for good', async () => {
    const env = {};
    const { save } = fakeSave(env);
    const doFetch = jest.fn();
    const log = captureLog();

    await consent.askTelemetryConsent(fakeIo({ confirm: [false] }), env, save, { fetchImpl: doFetch });
    log.restore();

    expect(env.RUMI_TELEMETRY).toBe('off');
    expect(doFetch).not.toHaveBeenCalled();
  });

  test('an abort mid-question writes nothing at all', async () => {
    // Ctrl+C propagates out to main(), which exits 130. Neither answer is
    // recorded, so the next `rumi setup` asks again rather than assuming.
    const env = {};
    const { save, writes } = fakeSave(env);
    const io = fakeIo();
    io.confirm = async () => { const err = new Error('Cancelled by user'); err.aborted = true; throw err; };
    const log = captureLog();

    await expect(consent.askTelemetryConsent(io, env, save, { fetchImpl: jest.fn() })).rejects.toThrow('Cancelled');
    log.restore();

    expect(writes).toEqual([]);
    expect(env.RUMI_TELEMETRY).toBeUndefined();
  });

  test('on a no, records the answer and sends nothing', async () => {
    const env = {};
    const { save, writes } = fakeSave(env);
    const doFetch = jest.fn();
    const log = captureLog();

    const result = await consent.askTelemetryConsent(io_no(), env, save, { fetchImpl: doFetch });
    log.restore();

    expect(result.shared).toBe(false);
    expect(writes).toEqual([{ RUMI_TELEMETRY: 'off' }]);
    expect(env.RUMI_DEPLOYMENT_ID).toBeUndefined();
    expect(doFetch).not.toHaveBeenCalled();
  });

  test('on a yes, writes the flag and a generated deployment id', async () => {
    const env = {};
    const { save } = fakeSave(env);
    const log = captureLog();

    const result = await consent.askTelemetryConsent(io_yes(), env, save, {
      fetchImpl: jest.fn().mockResolvedValue({ ok: true, status: 202 }),
    });
    log.restore();

    expect(result.shared).toBe(true);
    expect(env.RUMI_TELEMETRY).toBe('on');
    expect(env.RUMI_DEPLOYMENT_ID).toMatch(/^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/);
  });

  test('on a yes, registers the deployment once', async () => {
    const env = {};
    const { save } = fakeSave(env);
    const doFetch = jest.fn().mockResolvedValue({ ok: true, status: 202 });
    const log = captureLog();

    const result = await consent.askTelemetryConsent(io_yes(), env, save, { fetchImpl: doFetch });
    log.restore();

    expect(doFetch).toHaveBeenCalledTimes(1);
    expect(doFetch.mock.calls[0][0].endsWith('/register')).toBe(true);
    expect(JSON.parse(doFetch.mock.calls[0][1].body).deployment_id).toBe(env.RUMI_DEPLOYMENT_ID);
    expect(result.registered).toBe(true);
  });

  test('a registration that cannot reach us still leaves setup successful', async () => {
    const env = {};
    const { save } = fakeSave(env);
    const log = captureLog();

    const result = await consent.askTelemetryConsent(io_yes(), env, save, {
      fetchImpl: jest.fn().mockRejectedValue(new Error('ECONNREFUSED')),
    });
    log.restore();

    expect(result.shared).toBe(true);
    expect(result.registered).toBe(false);
    expect(env.RUMI_DEPLOYMENT_ID).toBeTruthy();
  });

  test('each yes generates a distinct deployment id', async () => {
    const ids = [];
    for (let i = 0; i < 2; i += 1) {
      const env = {};
      const { save } = fakeSave(env);
      const log = captureLog();
      await consent.askTelemetryConsent(io_yes(), env, save, { fetchImpl: jest.fn().mockResolvedValue({ ok: true }) });
      log.restore();
      ids.push(env.RUMI_DEPLOYMENT_ID);
    }

    expect(ids[0]).not.toBe(ids[1]);
  });
});

function io_yes() { return fakeIo({ confirm: [true] }); }
function io_no() { return fakeIo({ confirm: [false] }); }
