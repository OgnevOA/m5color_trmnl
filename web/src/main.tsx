import React from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import { LightboxProvider, ToastProvider } from "./ui";
import "./theme.css";

createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <ToastProvider>
      <LightboxProvider>
        <App />
      </LightboxProvider>
    </ToastProvider>
  </React.StrictMode>,
);
