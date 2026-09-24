import { readFile } from "node:fs/promises";
import { existsSync } from "node:fs";
import { dirname, join } from "node:path";

/**
 * True when the region belongs to the aws-cn partition (cn-north-1,
 * cn-northwest-1). Features whose backing services are not reachable from
 * that partition (template model providers, Bedrock Agent import, the
 * telemetry collector) gate on this.
 */
export function isChinaRegion(region: string): boolean {
  return region.startsWith("cn-");
}

/**
 * True when the CLI is operating in a China (aws-cn) context: the ambient AWS
 * region env vars point at cn-*, or the AgentCore project enclosing `cwd`
 * declares a cn-* deployment target in agentcore/aws-targets.json.
 *
 * Best-effort by design — a missing project or an unreadable/malformed targets
 * file counts as non-China — so callers that must never fail (telemetry) can
 * rely on it unconditionally.
 */
export async function isChinaContext(cwd: string = process.cwd()): Promise<boolean> {
  if (isChinaRegion(process.env.AWS_REGION ?? process.env.AWS_DEFAULT_REGION ?? "")) return true;

  // Mirror project discovery: walk up to the first directory containing
  // agentcore/agentcore.json, then inspect its aws-targets.json.
  for (let dir = cwd; ; dir = dirname(dir)) {
    if (existsSync(join(dir, "agentcore", "agentcore.json"))) {
      try {
        const raw = await readFile(join(dir, "agentcore", "aws-targets.json"), "utf8");
        const targets: unknown = JSON.parse(raw);
        return (
          Array.isArray(targets) &&
          targets.some(
            (target) =>
              typeof target === "object" &&
              target !== null &&
              "region" in target &&
              typeof target.region === "string" &&
              isChinaRegion(target.region),
          )
        );
      } catch {
        return false;
      }
    }
    if (dirname(dir) === dir) return false;
  }
}
