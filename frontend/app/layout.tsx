import "@xterm/xterm/css/xterm.css";
import "./globals.css";
import type { Metadata } from "next";
import { Anton, Archivo } from "next/font/google";
import { VoiceProvider } from "@/components/VoiceProvider";
import { ConversationRail } from "@/components/ConversationRail";
import { Nav } from "@/components/shell/Nav";

const display = Anton({ weight: "400", subsets: ["latin"], variable: "--font-display" });
const body = Archivo({ subsets: ["latin"], variable: "--font-body" });

export const metadata: Metadata = {
  title: "Yap Code",
  description: "Hands-free voice control for Claude Code",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  // suppressHydrationWarning: browser extensions inject attributes onto
  // <html>/<body> (e.g. __gcrremoteframetoken) that aren't in the server HTML,
  // which otherwise triggers a dev hydration-mismatch overlay. This only
  // suppresses attribute diffs on these two elements, not their children.
  //
  // VoiceProvider and ConversationRail live HERE, above the routed
  // {children} — not inside a page. A layout persists across route changes;
  // a route does not. So navigating between nav items re-renders only
  // <main>, and the voice connection plus both SSE subscriptions never
  // unmount. Get this backwards and Yuri drops mid-sentence on every click.
  return (
    <html lang="en" suppressHydrationWarning className={`${display.variable} ${body.variable}`}>
      <body suppressHydrationWarning>
        <VoiceProvider>
          <div className="shell">
            <Nav />
            <main className="shell-main">{children}</main>
            <ConversationRail />
          </div>
        </VoiceProvider>
      </body>
    </html>
  );
}
