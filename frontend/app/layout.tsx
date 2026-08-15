import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Quorum",
  description: "A supervisor agent that reviews pull requests with citation-backed findings.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
