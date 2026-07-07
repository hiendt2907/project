import { NextRequest, NextResponse } from "next/server";

// CSP dựa trên NONCE mỗi request cho SCRIPT (không unsafe-inline/eval). Next tự
// gắn nonce vào script bootstrap của nó khi thấy CSP trong request header.
// style-src cần 'unsafe-inline': mermaid.js (dùng để render sơ đồ /understanding,
// xem components/mermaid-diagram.tsx) tự sinh <style> + style="" inline lúc
// render client-side và KHÔNG hỗ trợ CSP nonce — style-src 'self' đơn thuần
// khiến trình duyệt âm thầm bỏ qua toàn bộ style đó (không có lỗi console rõ
// ràng), sơ đồ render ra hộp đen mặc định SVG (fill:black), không viền, không
// màu — đã xác minh qua getComputedStyle thật (computed fill luôn rgb(0,0,0)
// dù style attribute đúng giá trị mong muốn). 'unsafe-inline' chỉ nới style,
// script-src vẫn strict-nonce — theo đúng khuyến nghị CSP chuẩn
// (~/.claude/rules/web/security.md: `style-src 'self' 'unsafe-inline' ...`).
// Đây là CSP RIÊNG của Provider Portal.
export function middleware(request: NextRequest) {
  const nonce = Buffer.from(crypto.randomUUID()).toString("base64");
  const csp = [
    `default-src 'self'`,
    `script-src 'self' 'nonce-${nonce}' 'strict-dynamic'`,
    `style-src 'self' 'unsafe-inline'`,
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
