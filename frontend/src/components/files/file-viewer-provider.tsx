"use client";

import { usePathname, useRouter } from "next/navigation";
import { useCallback, useMemo, useState, type ReactNode } from "react";
import { Drawer } from "@/components/ui/overlay";
import { viewerHref, type FileSource } from "@/lib/files";
import { FileViewer } from "./file-viewer";
import { FileViewerContext } from "./file-viewer-context";

export { useFileViewer, useOptionalFileViewer } from "./file-viewer-context";

/**
 * App-wide file viewer: `useFileViewer().openFile({ artifactId } | { path })` opens a wide side drawer
 * from chat, tasks, schedules or outputs. Mounted once inside the authenticated shell.
 */
export function FileViewerProvider({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  const router = useRouter();
  // Open "for" a pathname, so navigating elsewhere hides the drawer without an effect.
  const [state, setState] = useState<{ source: FileSource; at: string } | null>(null);

  const openFile = useCallback((source: FileSource) => setState({ source, at: pathname }), [pathname]);
  const closeFile = useCallback(() => setState(null), []);
  const value = useMemo(() => ({ openFile, closeFile }), [openFile, closeFile]);
  const open = !!state && state.at === pathname;

  return (
    <FileViewerContext.Provider value={value}>
      {children}
      <Drawer open={open} onOpenChange={(o) => !o && closeFile()} width={820} bare aria-label="File viewer">
        {state && (
          <FileViewer
            // Fresh state (HTML source toggle, media URL) per file.
            key={"artifactId" in state.source ? `a:${state.source.artifactId}` : `p:${state.source.path}`}
            source={state.source}
            layout="drawer"
            onClose={closeFile}
            onNavigate={closeFile}
            onOpenFullPage={() => {
              const href = viewerHref(state.source);
              closeFile();
              router.push(href);
            }}
          />
        )}
      </Drawer>
    </FileViewerContext.Provider>
  );
}
