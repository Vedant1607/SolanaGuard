/**
 * Some browsers and saved bookmarks request /favicon.png even though the
 * application ships the canonical icon as /favicon.ico.
 *
 * Keep this legacy path as a route handler (not a page) so a missing static
 * asset never falls through the root layout's Clerk user-sync path.
 */
export function GET(request: Request) {
  return Response.redirect(new URL("/favicon.ico", request.url), 308);
}
