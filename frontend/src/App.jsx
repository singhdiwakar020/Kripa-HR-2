import React, { useEffect, useState } from "react";
import SearchPage from "./SearchPage.jsx";
import UploadPage from "./UploadPage.jsx";
import CallsPage from "./CallsPage.jsx";
import TestsPage from "./TestsPage.jsx";
import { api } from "./api.js";

const PhoneIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 16.9v3a2 2 0 0 1-2.2 2 19.8 19.8 0 0 1-8.6-3.1 19.5 19.5 0 0 1-6-6A19.8 19.8 0 0 1 2.1 4.2 2 2 0 0 1 4.1 2h3a2 2 0 0 1 2 1.7c.1 1 .4 1.9.7 2.8a2 2 0 0 1-.5 2.1L8.1 9.9a16 16 0 0 0 6 6l1.3-1.3a2 2 0 0 1 2.1-.4c.9.3 1.8.6 2.8.7a2 2 0 0 1 1.7 2z" /></svg>
);

const TestIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M9 11l2 2 4-4" /><rect x="4" y="3" width="16" height="18" rx="2" /></svg>
);

const SearchIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="11" cy="11" r="7" /><path d="m20 20-3.5-3.5" /></svg>
);
const UploadIcon = () => (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 16V4m0 0-4 4m4-4 4 4" /><path d="M4 16v3a1 1 0 0 0 1 1h14a1 1 0 0 0 1-1v-3" /></svg>
);

export default function App() {
  const [tab, setTab] = useState("search");
  const [dataVersion, setDataVersion] = useState(0);
  const [total, setTotal] = useState(null);

  useEffect(() => {
    api.facets().then((f) => setTotal(f.total)).catch(() => {});
  }, [dataVersion]);

  return (
    <div className="app">
      <header className="topbar">
        <div className="brand"><div className="logo">K</div><div>Kripa <span>HR</span></div></div>
        <nav>
          <button className={tab === "search" ? "active" : ""} onClick={() => setTab("search")}><SearchIcon /> Search</button>
          <button className={tab === "calls" ? "active" : ""} onClick={() => setTab("calls")}><PhoneIcon /> Calls</button>
          <button className={tab === "tests" ? "active" : ""} onClick={() => setTab("tests")}><TestIcon /> Tests</button>
          <button className={tab === "upload" ? "active" : ""} onClick={() => setTab("upload")}><UploadIcon /> Upload</button>
        </nav>
        {total != null && <div className="total">{total} candidates</div>}
      </header>
      <main>
        {tab === "search" ? (
          <SearchPage key={dataVersion} goUpload={() => setTab("upload")} goCalls={() => setTab("calls")} goTests={() => setTab("tests")} />
        ) : tab === "calls" ? (
          <CallsPage />
        ) : tab === "tests" ? (
          <TestsPage />
        ) : (
          <UploadPage
            onImported={() => {
              setDataVersion((v) => v + 1);
              setTab("search");
            }}
          />
        )}
      </main>
    </div>
  );
}
