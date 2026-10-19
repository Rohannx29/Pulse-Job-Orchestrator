import { NextResponse } from "next/server";
import type { NextRequest } from "next/server";

// Paths that do NOT require authentication
const PUBLIC_PATHS = ["/login", "/signup", "/accept-invite"];


export function middleware(request: NextRequest) {
  const { pathname } = request.nextUrl;

  // Always allow public paths
  if (PUBLIC_PATHS.some((p) => pathname.startsWith(p))) {
    return NextResponse.next();
  }

  // Check for the access token cookie (set as httpOnly by the API).
  // Middleware runs on the edge before any page renders, so there is no
  // client-side flash of protected content before the redirect fires.
  const token = request.cookies.get("pulse_access_token");
  if (!token) {
    const loginUrl = new URL("/login", request.url);
    // Preserve the originally-requested path so we can redirect back after login
    loginUrl.searchParams.set("next", pathname);
    return NextResponse.redirect(loginUrl);
  }

  return NextResponse.next();
}

export const config = {
  // Match all routes except Next.js internals and static assets
  matcher: ["/((?!_next/static|_next/image|favicon.ico).*)"],
};
