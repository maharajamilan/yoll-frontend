import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

/**
 * Edge proxy that gates the entire site behind a single shared password
 * (HTTP Basic Auth). The password lives in SITE_PASSWORD env var; if it's
 * unset, the gate is disabled (e.g. for local dev). Username is fixed to
 * "yyp" — Basic Auth requires both fields, but only the password is checked.
 *
 * In Next.js 16 this convention is `proxy.ts` (not `middleware.ts`).
 */
export function proxy(request: NextRequest) {
  const password = process.env.SITE_PASSWORD;
  if (!password) {
    return NextResponse.next();
  }

  const auth = request.headers.get("authorization");
  if (auth) {
    const [scheme, encoded] = auth.split(" ");
    if (scheme === "Basic" && encoded) {
      try {
        const decoded = atob(encoded);
        const idx = decoded.indexOf(":");
        const supplied = idx >= 0 ? decoded.slice(idx + 1) : decoded;
        if (supplied === password) {
          return NextResponse.next();
        }
      } catch {
        // Fall through to 401
      }
    }
  }

  return new NextResponse("Authentication required", {
    status: 401,
    headers: {
      "WWW-Authenticate": 'Basic realm="Yale Youth Poll Crosstab Explorer"',
    },
  });
}

export const config = {
  // Run on every path except Next's own static asset bundle and image
  // optimizer (which are not security-sensitive on their own and we'd just
  // double the auth round-trips).
  matcher: ["/((?!_next/static|_next/image|favicon\\.ico).*)"],
};
