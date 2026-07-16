import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import React from "react";
import ReactDOM from "react-dom/client";
import { createBrowserRouter, RouterProvider } from "react-router-dom";

import App from "./App";
import MatchList from "./pages/MatchList";
import MatchPage from "./pages/MatchPage";
import DraftPage from "./pages/DraftPage";
import MetaPage from "./pages/MetaPage";
import PlayerPage from "./pages/PlayerPage";
import "./styles.css";

const queryClient = new QueryClient({
  defaultOptions: { queries: { staleTime: 30_000, retry: 1 } },
});

const router = createBrowserRouter([
  {
    path: "/",
    element: <App />,
    children: [
      { index: true, element: <MatchList /> },
      { path: "matches/:matchId", element: <MatchPage /> },
      { path: "players/:playerId", element: <PlayerPage /> },
      { path: "meta", element: <MetaPage /> },
      { path: "draft", element: <DraftPage /> },
    ],
  },
]);

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <RouterProvider router={router} />
    </QueryClientProvider>
  </React.StrictMode>,
);
