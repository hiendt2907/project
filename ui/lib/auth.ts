import type { NextAuthOptions } from "next-auth";
import CredentialsProvider from "next-auth/providers/credentials";
import { getRedis } from "./redis";

const nextAuthUrl = process.env.NEXTAUTH_URL ?? "";
const useSecureCookies = nextAuthUrl.startsWith("https://");

/**
 * NextAuth v4 typings omit `trustHost`; runtime honours it for multi-host / proxy setups.
 */
export const authOptions: NextAuthOptions & { trustHost?: boolean } = {
  trustHost: true,
  useSecureCookies,
  secret: process.env.NEXTAUTH_SECRET,
  providers: [
    CredentialsProvider({
      name: "credentials",
      credentials: {
        username: { label: "Username", type: "text" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        const validUser = process.env.ADMIN_USERNAME;
        const validPass = process.env.ADMIN_PASSWORD;
        if (!validUser || !validPass) {
          throw new Error("ADMIN_USERNAME and ADMIN_PASSWORD env vars must be set");
        }

        // Rate limit: 5 attempts per username per 60s
        const username = credentials?.username ?? "";
        try {
          const redis = getRedis();
          const key = `login:attempts:${username}`;
          const attempts = await redis.incr(key);
          if (attempts === 1) await redis.expire(key, 60);
          if (attempts > 5) return null;
        } catch {
          // Redis unavailable — degrade gracefully, allow login attempt
        }

        if (
          credentials?.username === validUser &&
          credentials?.password === validPass
        ) {
          return { id: "1", name: "Admin", email: "admin@omni.local", role: "admin" };
        }
        return null;
      },
    }),
  ],
  pages: {
    signIn: "/login",
  },
  callbacks: {
    async session({ session, token }) {
      if (token && session.user) {
        session.user.id = token.sub as string;
        session.user.role = token.role as string;
      }
      return session;
    },
    async jwt({ token, user }) {
      if (user) {
        token.role = user.role;
      }
      return token;
    },
  },
  session: {
    strategy: "jwt",
  },
  cookies: {
    sessionToken: {
      name: useSecureCookies
        ? "__Secure-next-auth.session-token"
        : "next-auth.session-token",
      options: {
        httpOnly: true,
        sameSite: "lax",
        path: "/",
        secure: useSecureCookies,
        domain: process.env.AUTH_COOKIE_DOMAIN?.trim() || undefined,
      },
    },
  },
};
