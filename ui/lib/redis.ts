import Redis from "ioredis";

declare global {
  // eslint-disable-next-line no-var
  var __omniRedis: Redis | undefined;
}

export function getRedis(): Redis {
  if (!global.__omniRedis) {
    const url = process.env.OMNI_REDIS_URL || "redis://127.0.0.1:6379";
    global.__omniRedis = new Redis(url, { lazyConnect: false, maxRetriesPerRequest: 2 });
  }
  return global.__omniRedis;
}
