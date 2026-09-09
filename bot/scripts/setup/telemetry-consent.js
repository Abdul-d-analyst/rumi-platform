/**
 * The one question in setup that is asked on someone else's behalf.
 *
 * Rumi is open source, so the people maintaining it cannot see how any
 * deployment is going unless its operator chooses to tell them. This asks in
 * plain words, directly under the full disclosure of what is and is not sent.
 * The default is yes; "n" declines for good, and Ctrl+C declines for now.
 *
 * Saying yes writes two things to .env — the flag and a freshly generated
 * deployment id — and then registers the deployment, so it is counted even if
 * the bot is never started. Saying no writes the flag as `off`, which is what
 * stops `rumi setup` asking again every time it is resumed.
 *
 * Lives under bot/scripts/ rather than bot/shared/ because this is operator-
 * facing copy that names the project's stewards, which shared bot code may not.
 */

const crypto = require('crypto');
const ui = require('./ui');

/**
 * The disclosure, kept as data so a test can assert the promises it makes are
 * still the promises the code keeps. Every line here is a commitment: if the
 * payload in bot/shared/utils/telemetry.js grows, this has to grow with it.
 */
const CONSENT_COPY = {
  question: 'Would you be comfortable sharing basic usage stats with Taleemabad?',
  why: 'Rumi is free and open source. Knowing how it actually gets used is how we decide what to fix and what to build next.',
  sentHeading: 'What would be sent, once a day — three numbers, and nothing else:',
  sent: [
    'how many teachers are registered, and how many were active this week',
    'how many lesson plans were made in the last 30 days',
    'plus a random ID for this deployment, the Rumi version you run, and which features you have switched on',
  ],
  neverHeading: 'What would never be sent:',
  never: [
    'nothing about any individual — no names, no phone numbers, no teacher or student records',
    'no message content, no lesson plans, no recordings, no scores',
    'no API keys, and nothing else from your database',
  ],
  optOut: 'You can turn this off at any time: set RUMI_TELEMETRY=off in your .env file.',
  prompt: 'Share stats?',
};

/** Prints the disclosure. Separated so the wording can be tested on its own. */
function printConsentCopy() {
  console.log(ui.say(CONSENT_COPY.why));
  console.log('');
  console.log(`  ${ui.bold(CONSENT_COPY.sentHeading)}`);
  for (const line of CONSENT_COPY.sent) console.log(ui.bullet(line));
  console.log('');
  console.log(`  ${ui.bold(CONSENT_COPY.neverHeading)}`);
  for (const line of CONSENT_COPY.never) console.log(ui.bullet(line));
  console.log('');
  console.log(ui.aside(CONSENT_COPY.optOut));
  console.log('');
}

/**
 * Asks, records the answer in .env, and registers the deployment on a yes.
 *
 * @param {object} io       prompt.js's createIo() — or the test double
 * @param {object} env      the wizard's working env, mutated by `save`
 * @param {Function} save   createSaver()'s closure; writes through to .env
 * @param {object} [deps]   `{fetchImpl, version}` for tests
 * @returns {Promise<{shared: boolean, deploymentId?: string, registered?: boolean}>}
 */
async function askTelemetryConsent(io, env, save, deps = {}) {
  printConsentCopy();

  // Defaults to yes, so the prompt reads [Y/n]. Still an opt-in: the question
  // is put plainly, right under the full disclosure of what is and is not
  // sent, and a single "n" declines for good. Turning it off later is one
  // line in .env, named in the copy above.
  const shared = await io.confirm(CONSENT_COPY.prompt, true);

  if (!shared) {
    save({ RUMI_TELEMETRY: 'off' });
    console.log(ui.ok('Nothing will be shared. Rumi works exactly the same either way.'));
    return { shared: false };
  }

  const deploymentId = crypto.randomUUID();
  save({ RUMI_TELEMETRY: 'on', RUMI_DEPLOYMENT_ID: deploymentId });

  const registered = await registerDeployment(env, deps);
  console.log(ui.ok('Thank you — three numbers a day, and nothing else.'));
  return { shared: true, deploymentId, registered };
}

/**
 * Fires the one-off registration. A failure here is not worth mentioning to
 * the operator: the daily push will register the deployment on its first run
 * anyway, and setup has nothing to fix.
 */
async function registerDeployment(env, { fetchImpl, version } = {}) {
  try {
    const { sendRegistration } = require('../../shared/utils/telemetry');
    const result = await sendRegistration({ env, version: version || resolveVersion(), fetchImpl });
    return Boolean(result && result.ok);
  } catch (err) {
    return false;
  }
}

/** The version the deployment is running, or null if it cannot be read. */
function resolveVersion() {
  try {
    return require('../../package.json').version || null;
  } catch (err) {
    return null;
  }
}

module.exports = { askTelemetryConsent, printConsentCopy, registerDeployment, CONSENT_COPY };
