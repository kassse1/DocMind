import "@testing-library/jest-dom/vitest";

import { cleanup } from "@testing-library/react";
import { afterEach } from "vitest";

import { resetForTests } from "@/lib/auth/session";

afterEach(() => {
  cleanup();
  resetForTests();
});
