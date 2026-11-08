import { describe, expect, it } from "vitest";
import { readLocalRuntimeVars } from "../src/server/runtime-env";

describe("local Worker runtime variables", () => {
  it("forwards the operator token of the server claims to the local Worker", () => {
    // Wrangler keeps a development variable only when its name is a `vars` entry or a required
    // secret. The token is neither, so a local run dropped it and every claim request answered 401.
    const variables = readLocalRuntimeVars({ SERVER_CLAIMS_ADMIN_TOKEN: "local-operator-token" });
    expect(variables.SERVER_CLAIMS_ADMIN_TOKEN).toBe("local-operator-token");
  });

  it("keeps the token out of the local Worker when the environment has none", () => {
    expect(readLocalRuntimeVars({}).SERVER_CLAIMS_ADMIN_TOKEN).toBeUndefined();
  });

  it("blanks the mail settings of a local run and keeps the development code off", () => {
    const variables = readLocalRuntimeVars({
      AUTH_EXPOSE_DEVELOPMENT_CODE: "false",
      EMAIL_SMTP_HOST: "",
      EMAIL_SMTP_PORT: "",
      EMAIL_SMTP_USERNAME: "",
      EMAIL_SMTP_PASSWORD: "",
      EMAIL_FROM: "",
    });
    expect(variables.AUTH_EXPOSE_DEVELOPMENT_CODE).toBe("false");
    expect(variables.EMAIL_SMTP_HOST).toBe("");
    expect(variables.EMAIL_SMTP_PORT).toBe("");
    expect(variables.EMAIL_SMTP_USERNAME).toBe("");
    expect(variables.EMAIL_SMTP_PASSWORD).toBe("");
    expect(variables.EMAIL_FROM).toBe("");
  });

  it("reads a flag as a boolean and leaves an encrypted value out", () => {
    expect(readLocalRuntimeVars({ AUTH_EXPOSE_DEVELOPMENT_CODE: "true" }).AUTH_EXPOSE_DEVELOPMENT_CODE).toBe("true");
    expect(readLocalRuntimeVars({ SITE_PUBLISH_ENABLED: "yes" }).SITE_PUBLISH_ENABLED).toBe("true");
    expect(readLocalRuntimeVars({ SITE_PUBLISH_ENABLED: "no" }).SITE_PUBLISH_ENABLED).toBe("false");
    expect(readLocalRuntimeVars({ SKILLS_ADMIN_TOKEN: "encrypted:ciphertext" }).SKILLS_ADMIN_TOKEN).toBeUndefined();
  });
});
