import type { ConversationStatus } from "@/lib/types";

export const BUSY_STATUSES: ConversationStatus[] = ["thinking", "responding", "using_tool"];

export const isBusy = (s: ConversationStatus | null | undefined) => !!s && BUSY_STATUSES.includes(s);

export function statusLine(status: ConversationStatus | null | undefined, detail?: string | null): string | null {
  if (!status || status === "idle") return null;
  if (detail) return detail.endsWith("…") || detail.endsWith("...") ? detail : `${detail}…`;
  switch (status) {
    case "thinking":
      return "Ren is thinking…";
    case "responding":
      return "Ren is responding…";
    case "using_tool":
      return "Ren is working with tools…";
    default:
      return null;
  }
}

export const SUGGESTIONS: { title: string; prompt: string; tag: string }[] = [
  {
    tag: "Research",
    title: "Research our top 3 competitors and draft a brief",
    prompt: "Research our top 3 competitors and draft a one-page brief comparing positioning, pricing and recent moves.",
  },
  {
    tag: "Knowledge",
    title: "Summarize the uploaded policy and list obligations",
    prompt: "Summarize the most recently uploaded policy document and list every obligation it places on employees, with citations.",
  },
  {
    tag: "Data",
    title: "Analyze this spreadsheet and chart revenue by region",
    prompt: "Analyze the attached spreadsheet and chart revenue by region. Call out the biggest movers quarter over quarter.",
  },
  {
    tag: "Prep",
    title: "Prepare me for tomorrow's board meeting",
    prompt: "Prepare me for tomorrow's board meeting: key metrics, likely questions, and a short narrative I can open with.",
  },
  {
    tag: "Writing",
    title: "Draft a customer email about the pricing change",
    prompt: "Draft a clear, friendly customer email announcing our pricing change, including what changes, when, and why.",
  },
  {
    tag: "Answers",
    title: "Find the answer in our knowledge base: …",
    prompt: "Find the answer in our knowledge base: ",
  },
];
