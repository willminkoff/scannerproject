import React from "react";
import { UIProvider } from "./context/UIContext";
import AppRoutes from "./routes/AppRoutes";

const css = `
  * { box-sizing: border-box; }
  body {
    margin: 0;
    font-family: Tahoma, Verdana, sans-serif;
    background: #101317;
    color: #e9eef5;
  }
  .app-shell {
    min-height: 100vh;
    max-width: 520px;
    margin: 0 auto;
    padding: 12px;
  }
  .screen {
    background: #1b2129;
    border: 1px solid #2b3441;
    border-radius: 10px;
    padding: 14px;
  }
  .header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
  }
  .header h1 {
    font-size: 1.1rem;
    margin: 0;
  }
  .header .sub {
    color: #9fb0c7;
    font-size: 0.85rem;
  }
  .btn {
    border: 1px solid #3f4f65;
    background: #2a3647;
    color: #e9eef5;
    border-radius: 8px;
    padding: 8px 12px;
    cursor: pointer;
    font-size: 0.9rem;
  }
  .btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .btn-secondary {
    background: #232b35;
  }
  .btn-danger {
    background: #5f2631;
    border-color: #7d3442;
  }
  .button-row {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
    margin-top: 12px;
  }
  .field-grid {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 10px;
  }
  .card {
    border: 1px solid #2b3441;
    background: #11161d;
    border-radius: 8px;
    padding: 10px;
  }
  .menu-list,
  .checkbox-list,
  .list {
    display: grid;
    gap: 8px;
  }
  .input,
  .range {
    width: 100%;
    padding: 8px;
    border-radius: 8px;
    border: 1px solid #3f4f65;
    background: #0f141a;
    color: #e9eef5;
  }
  .row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
  }
  .muted {
    color: #9fb0c7;
  }
  .error {
    color: #ff7f90;
    margin-top: 8px;
  }
  .message {
    color: #7edc9f;
    margin-top: 8px;
  }
  .loading {
    padding: 20px 8px;
    text-align: center;
    color: #9fb0c7;
  }

  .hp2-main {
    padding: 0;
    border-radius: 12px;
    overflow: hidden;
    border: 1px solid #4b535f;
    background: linear-gradient(180deg, #0f1218 0%, #0a0e14 100%);
    box-shadow: 0 8px 30px rgba(0, 0, 0, 0.45);
  }
  .hp2-radio-bar {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 8px 10px;
    border-bottom: 1px solid #303844;
    background: linear-gradient(180deg, #232d3b 0%, #1b2430 100%);
  }
  .hp2-radio-buttons {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
  }
  .hp2-radio-btn {
    border: 1px solid #556175;
    background: #2d394b;
    color: #dbe6f5;
    border-radius: 4px;
    padding: 2px 6px;
    font-size: 0.75rem;
    cursor: pointer;
  }
  .hp2-radio-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-status-icons {
    display: flex;
    align-items: center;
    gap: 6px;
  }
  .hp2-icon {
    border: 1px solid #5a687b;
    border-radius: 4px;
    padding: 2px 5px;
    font-size: 0.68rem;
    color: #d7e2f2;
    background: #1a2431;
    min-width: 42px;
    text-align: center;
  }
  .hp2-icon.on {
    color: #ffe39a;
    border-color: #d0a34c;
    background: #3a2f1e;
  }
  .hp2-lines {
    padding: 8px;
  }
  .hp2-line {
    display: grid;
    grid-template-columns: 136px 1fr 26px;
    border: 1px solid #344050;
    border-radius: 5px;
    overflow: hidden;
    background: #121821;
    margin-bottom: 6px;
  }
  .hp2-line:last-child {
    margin-bottom: 0;
  }
  .hp2-line-label {
    padding: 8px 7px;
    border-right: 1px solid #344050;
    color: #b7c9df;
    font-size: 0.7rem;
    text-transform: uppercase;
    letter-spacing: 0.04em;
    display: flex;
    align-items: center;
    background: #18222f;
  }
  .hp2-line-body {
    padding: 7px 10px;
    min-height: 52px;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 3px;
  }
  .hp2-line-primary {
    font-size: 1.02rem;
    color: #ffb54a;
    line-height: 1.15;
  }
  .hp2-line-secondary {
    color: #9fb0c7;
    font-size: 0.78rem;
    line-height: 1.2;
  }
  .hp2-line.channel .hp2-line-primary {
    color: #ffe169;
  }
  .hp2-subtab {
    border: 0;
    border-left: 1px dashed #3a4656;
    background: #111821;
    color: #d2dced;
    font-size: 0.95rem;
    font-weight: bold;
    cursor: pointer;
  }
  .hp2-subtab:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-submenu-popup {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
    padding: 8px 10px;
    background: #131a24;
    border-top: 1px dashed #394657;
    border-bottom: 1px dashed #394657;
  }
  .hp2-submenu-btn {
    border: 1px solid #54637a;
    border-radius: 6px;
    background: #222e3f;
    color: #dbe7f8;
    padding: 5px 10px;
    font-size: 0.82rem;
    cursor: pointer;
  }
  .hp2-submenu-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-feature-bar {
    display: grid;
    grid-template-columns: repeat(4, minmax(0, 1fr));
    gap: 1px;
    background: #2a3342;
    border-top: 1px solid #3a4452;
  }
  .hp2-feature-btn {
    border: 0;
    background: #1b2431;
    color: #d7e2f5;
    font-size: 0.83rem;
    padding: 10px 6px;
    cursor: pointer;
  }
  .hp2-feature-btn:disabled {
    opacity: 0.55;
    cursor: not-allowed;
  }
  .hp2-web-audio {
    border-top: 1px solid #303a46;
    padding: 10px;
    background: #0d131c;
  }
  .hp2-audio-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 8px;
  }
  .hp2-source-switch {
    display: flex;
    gap: 8px;
    margin-bottom: 6px;
  }
  .hp2-audio-meta {
    margin: 6px 0 8px;
  }
  .hp2-audio-player {
    width: 100%;
  }
  @media (max-width: 520px) {
    .hp2-line {
      grid-template-columns: 118px 1fr 24px;
    }
    .hp2-line-primary {
      font-size: 0.95rem;
    }
    .hp2-radio-btn {
      font-size: 0.72rem;
      padding: 2px 5px;
    }
  }
`;

export default function App() {
  return (
    <UIProvider>
      <div className="app-shell">
        <style>{css}</style>
        <AppRoutes />
      </div>
    </UIProvider>
  );
}
