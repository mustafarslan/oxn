// Cognitive Complexity scores from eslint-plugin-sonarjs, for differential testing.
//
// LGPL-3.0, invoked as a subprocess and never linked or distributed with OXN -- see
// ADR-0001. Usage: node sonarjs_runner.mjs <file.js> <eslint.config.mjs>
import { ESLint } from "eslint";
import fs from "node:fs";

const source = fs.readFileSync(process.argv[2], "utf8");
const eslint = new ESLint({ overrideConfigFile: process.argv[3] });
const [result] = await eslint.lintText(source, { filePath: "t.js" });

const scores = result.messages
  .filter((m) => m.ruleId === "sonarjs/cognitive-complexity")
  .map((m) => {
    const match = /Complexity from (\d+) to/.exec(m.message);
    return match ? { line: m.line, score: Number(match[1]) } : null;
  })
  .filter(Boolean);

console.log(JSON.stringify(scores));
