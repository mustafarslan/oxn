// Threshold 0 makes every function report its score, turning a lint rule into an oracle.
import sonarjs from "eslint-plugin-sonarjs";

export default [
  {
    files: ["**/*.js"],
    plugins: { sonarjs },
    rules: { "sonarjs/cognitive-complexity": ["error", 0] },
  },
];
