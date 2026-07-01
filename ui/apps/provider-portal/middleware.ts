import { NextRequest, NextResponse } from "next/server";

// CSP dựa trên NONCE mỗi request (không unsafe-inline/eval). Next tự gắn nonce vào
// script bootstrap của nó khi thấy CSP trong request header. CSS là file ngoài same-origin
// nên style-src 'self' là đủ. Đây là CSP RIÊNG của Provider Portal.
export function middleware(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = [
    `default-src 'self'`,
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    `style-src 'self'`,
    `img-src 'self' data:`,
    `connect-src 'self'`,
    `frame-ancestors 'none'`,
    `object-src 'none'`,
    `base-uri 'self'`,
    `form-action 'self'`,
  ].join("; ");

  const requestHeaders = new Headers(request.headers);
  requestHeaders.set("x-nonce", nonce);
  requestHeaders.set("content-security-policy", csp);

  const response = NextResponse.next({ request: { headers: requestHeaders } });
  response.headers.set("content-security-policy", csp);
  response.headers.set("x-content-type-options", "nosniff");
  response.headers.set("x-frame-options", "DENY");
  response.headers.set("referrer-policy", "strict-origin-when-cross-origin");
  response.headers.set("permissions-policy", "camera=(), microphone=(), geolocation=()");
  response.headers.set("strict-transport-security", "max-age=31536000; includeSubDomains");
  return response;
}

export const config = {
  // Bỏ qua asset tĩnh + proxy /auth,/api (backend tự đặt header của nó).
  matcher: ["/((?!_next/static|_next/image|favicon.ico|auth|api).*)"],
};
