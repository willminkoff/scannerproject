import React from "react";

export default function Header({ title, subtitle = "", showBack = false, onBack }) {
  return (
    <div className="header">
      <div>
        <h1>{title}</h1>
        {subtitle ? <div className="sub">{subtitle}</div> : null}
      </div>
      {showBack ? (
        <button type="button" className="btn btn-secondary" onClick={onBack}>
          Back
        </button>
      ) : null}
    </div>
  );
}
