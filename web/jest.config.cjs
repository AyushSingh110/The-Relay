/** Jest + React Testing Library (plan V2-06). Runs offline: the API is a fake client. */
module.exports = {
  testEnvironment: "jsdom",
  roots: ["<rootDir>/src"],
  setupFilesAfterEnv: ["<rootDir>/src/test/setup.ts"],
  moduleNameMapper: {
    // import.meta.env exists only under Vite; tests use fixed values instead
    "^(\.{1,2}/)+buildConfig$": "<rootDir>/src/test/buildConfig.ts",
    "\.css$": "<rootDir>/src/test/style.ts",
  },
  testMatch: ["**/*.test.ts", "**/*.test.tsx"],
};
