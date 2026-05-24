import "./globals.css";
import type { Metadata } from "next";

export const metadata: Metadata = {
  title: "Voice-Claude",
  description: "Hands-free voice control for Claude Code",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
