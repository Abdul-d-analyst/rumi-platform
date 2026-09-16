/**
 * No-silent-mask CI-workflow conformance.
 *
 * GitHub Actions step bodies that end with `|| true` silently suppress
 * every non-zero exit code in the preceding pipeline. That's sometimes
 * intentional (a cleanup step that mustn't fail the run if the resource
 * was never created) but is most often a defensive mistake: a credential
 * grep `! grep CREDENTIAL_PATTERN files || true` always succeeds — the
 * `|| true` masks the very failure the step was added to catch.
 *
 * This guard locks the contract: every `|| true` in a CI workflow must
 * appear in the `ALLOWED_MASKS` set below, with a documented reason. New
 * masks added without a justification cause the test to fail; the
 * developer must either remove the mask or add an entry here explaining
 * why it's load-bearing.
 *
 * Scoped to `.github/workflows/*.yml`. Bash scripts elsewhere can use
 * `|| true` freely.
 */

const fs = require('fs');
const path = require('path');

const WORKFLOWS_DIR = path.resolve(__dirname, '../../.github/workflows');

// Each entry: { file, line, rationale }. Keep tight; prefer removing masks
// to allowlisting them. Each entry should explain what the mask is
// load-bearing for.
const ALLOWED_MASKS = [
  // data-standards.yml — "Run the shared validator (diff mode)" step.
  // The step's real pass/fail signal is `steps.validate.outputs.exit_code`,
  // captured from the FIRST validate_schema.py call two lines above; the
  // step then unconditionally `exit 0`s regardless, so nothing here can
  // affect the actual gating decision.
  {
    file: 'data-standards.yml',
    line: 131,
    rationale:
      "cat report.err >&2 || true — only relays stderr for visibility in the job log; cat's own exit code is irrelevant and the step exits 0 either way two lines later.",
  },
  {
    file: 'data-standards.yml',
    line: 135,
    rationale:
      "validate_schema.py --markdown ... || true — this second call only renders the Markdown report for the PR comment/artifact; the real pass/fail comes from the first call's captured exit_code. A failure here degrades gracefully: the PR-comment step wraps its report.md read in try/catch and posts a clear 'could not read the report file' message rather than crashing.",
  },
  // data-standards.yml — "Validate the report itself is well-formed" step.
  {
    file: 'data-standards.yml',
    line: 143,
    rationale:
      "validate_schema.py --mode validate-report ... || true — documented in the step's own trailing comment: report.json from --mode diff/staged/full is the findings-list shape validate_schema.py itself emits, not the fuller audit-report-format.md issue shape validate-report expects. This is a structural sanity spot-check, not a blocking requirement, until the template is adapted to emit the full issue-record shape end to end.",
  },
];

function findMasks() {
  if (!fs.existsSync(WORKFLOWS_DIR)) return [];
  const files = fs
    .readdirSync(WORKFLOWS_DIR)
    .filter((f) => f.endsWith('.yml') || f.endsWith('.yaml'));

  const masks = [];
  for (const file of files) {
    const text = fs.readFileSync(path.join(WORKFLOWS_DIR, file), 'utf-8');
    const lines = text.split('\n');
    lines.forEach((line, i) => {
      // Match `|| true` at end of line, ignoring trailing whitespace.
      if (/\|\|\s+true\s*$/.test(line)) {
        masks.push({ file, line: i + 1, content: line.trim() });
      }
    });
  }
  return masks;
}

describe('CI workflows — no silent `|| true` masks', () => {
  it('every `|| true` mask is documented in ALLOWED_MASKS', () => {
    const masks = findMasks();
    const unjustified = masks.filter(
      (m) =>
        !ALLOWED_MASKS.some(
          (allowed) => allowed.file === m.file && allowed.line === m.line
        )
    );

    expect(unjustified).toEqual([]);
  });
});
