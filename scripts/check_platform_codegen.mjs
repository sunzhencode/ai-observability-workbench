import { mkdtempSync, readFileSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { spawnSync } from "node:child_process";

const root = resolve(import.meta.dirname, "..");
const frontend = join(root, "operations-console");
const expected = join(frontend, "src", "api", "platform-schema.d.ts");
const temporary = mkdtempSync(join(tmpdir(), "incident-operations-codegen-"));
const generated = join(temporary, "platform-schema.d.ts");

try {
  const result = spawnSync(
    "npm",
    [
      "exec",
      "--",
      "openapi-typescript",
      "../backend/tests/platform_openapi_snapshot.json",
      "-o",
      generated,
    ],
    { cwd: frontend, encoding: "utf8" },
  );
  if (result.status !== 0) {
    process.stderr.write(result.stderr || result.stdout);
    process.exit(result.status ?? 1);
  }
  if (readFileSync(expected, "utf8") !== readFileSync(generated, "utf8")) {
    process.stderr.write(
      "Generated client is stale; run `npm --prefix operations-console run codegen`.\n",
    );
    process.exit(1);
  }
} finally {
  rmSync(temporary, { recursive: true, force: true });
}
