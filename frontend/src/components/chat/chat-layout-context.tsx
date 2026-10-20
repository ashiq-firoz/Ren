"use client";

import { createContext, useContext } from "react";

export interface ChatLayoutContextValue {
  /** Conversation list visible as a column (desktop). */
  listVisible: boolean;
  /** Toggle the list: collapses the column on desktop, opens a drawer on small screens. */
  toggleList: () => void;
}

export const ChatLayoutContext = createContext<ChatLayoutContextValue>({
  listVisible: true,
  toggleList: () => {},
});

export const useChatLayout = () => useContext(ChatLayoutContext);
