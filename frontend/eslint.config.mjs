import js from "@eslint/js";
import tseslint from "typescript-eslint";

// Flat config on ESLint 9. We lint with typescript-eslint's type-checked rules
// (strict, no `any`, no floating promises) rather than the legacy
// eslint-config-next, which pulls a patch that is incompatible with flat config.
export default tseslint.config(
  { ignores: [".next/**", "node_modules/**", "next-env.d.ts", "**/*.mjs"] },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  {
    languageOptions: {
      parserOptions: { projectService: true, tsconfigRootDir: import.meta.dirname },
    },
    rules: {
      "@typescript-eslint/no-explicit-any": "error",
      "@typescript-eslint/no-floating-promises": "error",
      "@typescript-eslint/no-misused-promises": "error",
    },
  },
);
