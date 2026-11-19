import "@testing-library/jest-dom";
import { TextDecoder, TextEncoder } from "node:util";
import { toHaveNoViolations } from "jest-axe";

expect.extend(toHaveNoViolations);
Object.assign(globalThis, { TextDecoder, TextEncoder });
