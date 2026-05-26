import "@xterm/xterm/css/xterm.css";
import "./globals.css";
import type { Metadata } from "next";
import { Anton, Archivo } from "next/font/google";

const display = Anton({ weight: "400", subsets: ["latin"], variable: "--font-display" });
const body = Archivo({ subsets: ["latin"], variable: "--font-body" });

export const metadata: Metadata = {
  title: "Voice-Claude",
  description: "Hands-free voice control for Claude Code",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // suppressHydrationWarning: browser extensions inject attributes onto
  // <html>/<body> (e.g. __gcrremoteframetoken) that aren't in the server HTML,
  // which otherwise triggers a dev hydration-mismatch overlay. This only
  // suppresses attribute diffs on these two elements, not their children.
  return (
    <html lang="en" suppressHydrationWarning className={`${display.variable} ${body.variable}`}>
      <body suppressHydrationWarning>{children}</body>
    </html>
  );
}
