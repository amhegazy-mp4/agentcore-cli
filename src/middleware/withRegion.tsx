import { RegionKey } from "../handlers/keys";
import { resolveRegion } from "../core/region";
import { type Middleware } from "../router";

// withRegion resolves the effective AWS region and pins it onto the context under
// RegionKey so the whole app uses a single, consistent value. There is always a
// region: it falls back to DEFAULT_REGION when nothing else is configured.
export function withRegion(): Middleware {
  return (h) => ({
    name: () => h.name(),
    description: () => h.description(),
    flags: () => h.flags(),
    arguments: () => h.arguments(),
    doesSupportTui: () => h.doesSupportTui(),
    children: () => h.children(),
    handle: async (ctx, flags, args) => {
      const region = await resolveRegion(ctx.value(RegionKey));
      await h.handle(ctx.withValue(RegionKey, region), flags, args);
    },
  });
}
