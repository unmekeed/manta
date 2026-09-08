import type { Metadata } from "next";
import { Geist, Geist_Mono } from "next/font/google";
import "./globals.css";
const geistSans = Geist({ variable: "--font-geist-sans", subsets: ["latin", "cyrillic"] });
const geistMono = Geist_Mono({ variable: "--font-geist-mono", subsets: ["latin", "cyrillic"] });
export const metadata: Metadata = { title: "Manta — разбор матчей Dota 2", description: "Вероятность победы, переломные моменты, карта и понятный разбор профессиональных матчей Dota 2." };
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) { return <html lang="ru" className="dark"><body className={`${geistSans.variable} ${geistMono.variable}`}>{children}</body></html>; }
