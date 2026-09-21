"use client";

import { ThemeProvider, useSystemThemeMode } from "@openuidev/react-ui";

export function Providers({ children }: { children: React.ReactNode }) {
  const mode = useSystemThemeMode();
  return (
    <ThemeProvider mode={mode}>
      <div className="dash-root">{children}</div>
    </ThemeProvider>
  );
}
