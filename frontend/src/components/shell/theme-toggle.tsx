"use client";

import { Moon, Sun } from "lucide-react";
import { IconButton, type IconButtonSize } from "@/components/ui/button";
import { useTheme } from "@/providers/theme";

/** One-click switch between the default beige theme and dark. */
export function ThemeToggle({ size = "sm", className }: { size?: IconButtonSize; className?: string }) {
  const { resolved, setTheme } = useTheme();
  const dark = resolved === "dark";
  return (
    <IconButton
      label={dark ? "Switch to beige theme" : "Switch to dark theme"}
      icon={dark ? <Sun /> : <Moon />}
      size={size}
      onClick={() => setTheme(dark ? "light" : "dark")}
      className={className}
    />
  );
}
