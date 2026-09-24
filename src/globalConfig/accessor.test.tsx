import { afterEach, beforeEach, describe, expect, test } from "bun:test";
import { mkdtemp, rm, writeFile } from "node:fs/promises";
import { join } from "node:path";
import { tmpdir } from "node:os";
import { DefaultGlobalConfigAccessor } from "./accessor";
import { FsReadWriteJson } from "../io";
import { createSilentLogger } from "../testing";

describe("DefaultGlobalConfigAccessor telemetry partition default", () => {
  let tempDir: string;
  let configPath: string;
  const savedEnv = { ...process.env };

  beforeEach(async () => {
    tempDir = await mkdtemp(join(tmpdir(), "agentcore-accessor-test-"));
    configPath = join(tempDir, "config.json");
    delete process.env.AWS_REGION;
    delete process.env.AWS_DEFAULT_REGION;
  });

  afterEach(async () => {
    process.env = { ...savedEnv };
    await rm(tempDir, { recursive: true, force: true });
  });

  function accessor() {
    const logger = createSilentLogger();
    return new DefaultGlobalConfigAccessor({
      logger,
      filePath: configPath,
      json: new FsReadWriteJson({ logger }),
    });
  }

  test("telemetry defaults to enabled outside the aws-cn partition", async () => {
    const config = await accessor().get();
    expect(config.telemetry.enabled).toBe(true);
  });

  test.each(["AWS_REGION", "AWS_DEFAULT_REGION"] as const)(
    "telemetry defaults to disabled when %s is a China region",
    async (envVar) => {
      process.env[envVar] = "cn-north-1";
      const config = await accessor().get();
      expect(config.telemetry.enabled).toBe(false);
    },
  );

  test("an explicit telemetry.enabled in the config file wins over the aws-cn default", async () => {
    process.env.AWS_REGION = "cn-north-1";
    await writeFile(configPath, JSON.stringify({ telemetry: { enabled: true } }));

    const config = await accessor().get();
    expect(config.telemetry.enabled).toBe(true);
  });
});
