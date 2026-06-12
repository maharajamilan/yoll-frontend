import { ImageResponse } from "next/og";
import { readFileSync } from "node:fs";
import { join } from "node:path";

// Run in Node so we can read the logo file from disk at build/request time.
export const runtime = "nodejs";

export const alt = "Yale Youth Poll — Data Explorer";
export const size = { width: 1200, height: 630 };
export const contentType = "image/png";

// Embed the YYP logo if it's been added at public/yyp-logo.png; otherwise the
// card renders text-only. Drop the PNG there and it appears automatically.
function loadLogo(): string | null {
  for (const name of ["yyp-logo.png", "yyp-logo-white.png"]) {
    try {
      const buf = readFileSync(join(process.cwd(), "public", name));
      return `data:image/png;base64,${buf.toString("base64")}`;
    } catch {
      // try next
    }
  }
  return null;
}

export default function Image() {
  const logo = loadLogo();
  return new ImageResponse(
    (
      <div
        style={{
          width: "100%",
          height: "100%",
          display: "flex",
          flexDirection: "column",
          justifyContent: "center",
          backgroundColor: "#00356b",
          padding: "90px",
          fontFamily: "sans-serif",
        }}
      >
        {logo ? (
          <img src={logo} height={130} style={{ marginBottom: 48 }} alt="" />
        ) : null}
        <div
          style={{
            fontSize: 34,
            letterSpacing: 6,
            color: "#9db8d8",
            fontWeight: 600,
          }}
        >
          YALE YOUTH POLL
        </div>
        <div
          style={{
            fontSize: 104,
            color: "#ffffff",
            fontWeight: 700,
            lineHeight: 1.05,
            marginTop: 8,
          }}
        >
          Data Explorer
        </div>
        <div style={{ fontSize: 38, color: "#c9d8ec", marginTop: 28 }}>
          Weighted crosstabs across every survey wave
        </div>
      </div>
    ),
    { ...size },
  );
}
