import React from "react";
import ReactDOM from "react-dom/client";
import App from "@/App";
import { LazyLoadBoundary } from "@/components/LazyLoadBoundary";
import "@/styles.css";

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <LazyLoadBoundary label="应用">
      <App />
    </LazyLoadBoundary>
  </React.StrictMode>,
);
