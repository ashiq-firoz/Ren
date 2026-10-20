"use client";

import { useParams, usePathname } from "next/navigation";
import { useMemo, useState, type ReactNode } from "react";
import { ChatLayoutContext } from "@/components/chat/chat-layout-context";
import { ConversationList } from "@/components/chat/conversation-list";
import { Drawer } from "@/components/ui/overlay";
import { useLocalPreference, useMediaQuery } from "@/hooks/use-browser";

export default function ChatLayout({ children }: { children: ReactNode }) {
  const params = useParams<{ id?: string }>();
  const pathname = usePathname();
  const desktop = useMediaQuery("(min-width: 1024px)", true);
  const [pref, setPref] = useLocalPreference<"open" | "closed">("ren_chat_list", "open");
  // Drawer is "open for" a path so navigating closes it.
  const [sheetFor, setSheetFor] = useState<string | null>(null);
  const sheetOpen = !desktop && sheetFor === pathname;
  const listVisible = desktop && pref === "open";

  const ctx = useMemo(
    () => ({
      listVisible,
      toggleList: () => {
        if (desktop) setPref(pref === "open" ? "closed" : "open");
        else setSheetFor(pathname);
      },
    }),
    [listVisible, desktop, pref, setPref, pathname],
  );

  return (
    <ChatLayoutContext.Provider value={ctx}>
      <div className="flex h-full min-h-0 flex-1 overflow-hidden">
        {listVisible && (
          <aside aria-label="Conversations" className="w-[260px] shrink-0 border-r border-border bg-bg">
            <ConversationList activeId={params.id} />
          </aside>
        )}
        {!desktop && (
          <Drawer open={sheetOpen} onOpenChange={(o) => !o && setSheetFor(null)} side="left" width={300} bare className="bg-bg">
            <ConversationList activeId={params.id} onNavigate={() => setSheetFor(null)} />
          </Drawer>
        )}
        <div className="flex min-w-0 flex-1 flex-col">{children}</div>
      </div>
    </ChatLayoutContext.Provider>
  );
}
