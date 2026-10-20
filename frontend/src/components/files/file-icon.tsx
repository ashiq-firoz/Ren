import { FILE_ICONS, fileIconKey } from "@/lib/files";
import type { FileKind } from "@/lib/types";
import { cn } from "@/lib/utils";

/** File-type glyph by kind (falls back to the extension of `name`). */
export function FileKindIcon({
  kind,
  name,
  className,
  strokeWidth = 1.75,
}: {
  kind?: FileKind | null;
  name?: string | null;
  className?: string;
  strokeWidth?: number;
}) {
  const Icon = FILE_ICONS[fileIconKey(kind, name)];
  return <Icon aria-hidden strokeWidth={strokeWidth} className={cn("shrink-0", className)} />;
}
