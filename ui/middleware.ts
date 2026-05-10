import { NextRequest, NextResponse } from "next/server";
import { getToken } from "next-auth/jwt";

const PROTECTED = ["/", "/playbooks", "/ledger"];

export async function middleware(req: NextRequest) {
  const { pathname, search } = req.nextUrl;
  if (!PROTECTED.includes(pathname)) return NextResponse.next();

  const token = await getToken({ req, secret: process.env.NEXTAUTH_SECRET });
  if (token) return NextResponse.next();

  const isRsc = req.headers.get("rsc") === "1" || req.nextUrl.searchParams.has("_rsc");
  const loginUrl = new URL(`/login?callbackUrl=${encodeURIComponent(pathname + search)}`, req.url);

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

export const config = {
  matcher: ["/", "/playbooks", "/ledger"],
};
