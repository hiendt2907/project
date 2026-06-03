import { NextRequest, NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";
import {
  OMNI_HOST,
  PORTAL_HOST,
  type OmniUiRealm,
  realmFromHost,
} from "@/lib/omni-ui-realm";

const OPS_PAGE_PREFIXES = ["/playbooks", "/ledger", "/kpi", "/incidents", "/siem", "/operator"];

const PORTAL_ADMIN_PREFIXES = [
  "/config",
  "/onboarding",
  "/deploy",
  "/workers",
  "/admin",
  "/remote-agents",
];

/** Host[:port] for redirects; K8s probes send Host=pod IP without port while hitting :3000. */
function requestAuthority(req: NextRequest): string {
  const hdr = req.headers.get("host");
  if (hdr?.includes(":")) return hdr;
  const port = req.nextUrl.port;
  if (port && port !== "80" && port !== "443") {
    return `${req.nextUrl.hostname}:${port}`;
  }
  return hdr ?? req.nextUrl.hostname;
}

/** Client-facing origin (Ingress), never the container :3000. */
function publicProtocol(req: NextRequest): "http:" | "https:" {
  const raw =
    req.headers.get("x-forwarded-proto")?.split(",")[0]?.trim() ?? "";
  return raw === "https" ? "https:" : "http:";
}

function matchesPrefix(pathname: string, prefixes: string[]): boolean {
  return prefixes.some(
    (p) => pathname === p || pathname.startsWith(`${p}/`),
  );
}

function absoluteUrl(
  req: NextRequest,
  authority: string,
  pathname: string,
  search: string,
): URL {
  const u = new URL(`${publicProtocol(req)}//${authority}`);
  u.pathname = pathname;
  u.search = search;
  return u;
}

function withRealm(req: NextRequest, realm: OmniUiRealm): NextResponse {
  if (realm === "local") return NextResponse.next();
  const requestHeaders = new Headers(req.headers);
  requestHeaders.set("x-omni-realm", realm);
  return NextResponse.next({
    request: { headers: requestHeaders },
  });
}

async function requireAuthOrLogin(
  req: NextRequest,
  pathname: string,
  search: string,
  realm: OmniUiRealm,
  publicHostname: string,
): Promise<NextResponse> {
  if (pathname.startsWith("/api/")) {
    const token = await getToken({ req, secret: process.env.NEXTAUTH_SECRET });
    if (token) return withRealm(req, realm);
    return NextResponse.json({ error: "Unauthorized" }, { status: 401 });
  }

  if (pathname === "/login") {
    return withRealm(req, realm);
  }

  const token = await getToken({ req, secret: process.env.NEXTAUTH_SECRET });
  if (token) return withRealm(req, realm);

  const authority =
    realm === "portal" || realm === "ops"
      ? publicHostname
      : requestAuthority(req);
  const loginUrl = absoluteUrl(req, authority, "/login", "");
  loginUrl.searchParams.set(
    "callbackUrl",
    `${pathname}${search}`,
  );
  const isRsc =
    req.headers.get("rsc") === "1" || req.nextUrl.searchParams.has("_rsc");
  if (isRsc) {
    return new NextResponse(null, {
      status: 200,
      headers: {
        "x-middleware-redirect": loginUrl.toString(),
        "x-nextjs-redirect": loginUrl.toString(),
        "content-type": "text/x-component",
      },
    });
  }
  return NextResponse.redirect(loginUrl);
}

export async function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  const hostHeader = req.headers.get("host") ?? "";
  const host = hostHeader.split(":")[0];
  const realm = realmFromHost(host);

  if (pathname.startsWith("/api/auth")) {
    return NextResponse.next();
  }

  if (realm === "portal" || realm === "ops") {
    if (realm === "portal") {
      if (pathname === "/") {
        return NextResponse.redirect(
          absoluteUrl(req, host, "/admin", search),
        );
      }
      if (matchesPrefix(pathname, OPS_PAGE_PREFIXES)) {
        return NextResponse.redirect(
          absoluteUrl(req, OMNI_HOST, pathname, search),
        );
      }
    } else {
      if (pathname === "/") {
        return NextResponse.redirect(
          absoluteUrl(req, host, "/operator", search),
        );
      }
      if (matchesPrefix(pathname, PORTAL_ADMIN_PREFIXES)) {
        return NextResponse.redirect(
          absoluteUrl(req, PORTAL_HOST, pathname, search),
        );
      }
    }
    return requireAuthOrLogin(req, pathname, search, realm, host);
  }

  return requireAuthOrLogin(req, pathname, search, "local", host);
}

export const config = {
  matcher: [
    "/((?!_next/static|_next/image|favicon.ico|.*\\.(?:svg|png|jpg|jpeg|gif|webp|ico)$).*)",
  ],
};
