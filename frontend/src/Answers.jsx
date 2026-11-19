import React from "react";
import { answerText } from "./util.js";

export function Answers({ answers }) {
  if (!answers?.length) return null;
  return (
    <dl className="answers">
      {answers.map((a, i) => <React.Fragment key={i}><dt>{a.question}</dt><dd>{answerText(a)}</dd></React.Fragment>)}
    </dl>
  );
}
