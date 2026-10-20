"use client";

import { MutationCache, QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { useState, type ReactNode } from "react";
import { Toaster } from "@/components/ui/toast";
import { ApiError, errorMessage } from "@/lib/api";
import { toast } from "@/lib/toast";
import { AuthProvider } from "./auth";
import { ThemeProvider } from "./theme";

function makeQueryClient() {
  return new QueryClient({
    defaultOptions: {
      queries: {
        staleTime: 30_000,
        gcTime: 10 * 60_000,
        refetchOnWindowFocus: false,
        retry: (failureCount, error) => {
          if (error instanceof ApiError) {
            if (error.status >= 400 && error.status < 500) return false;
            if (error.status === 0) return failureCount < 1;
          }
          return failureCount < 2;
        },
        retryDelay: (attempt) => Math.min(800 * 2 ** attempt, 6000),
      },
      mutations: { retry: false },
    },
    mutationCache: new MutationCache({
      onError: (error, _variables, _ctx, mutation) => {
        if (mutation.meta?.silent) return;
        toast.error(typeof mutation.meta?.errorTitle === "string" ? mutation.meta.errorTitle : "Request failed", {
          description: errorMessage(error),
        });
      },
    }),
  });
}

export function Providers({ children }: { children: ReactNode }) {
  const [queryClient] = useState(makeQueryClient);
  return (
    <QueryClientProvider client={queryClient}>
      <ThemeProvider>
        <AuthProvider>
          {children}
          <Toaster />
        </AuthProvider>
      </ThemeProvider>
    </QueryClientProvider>
  );
}
