import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { FormEvent, useState } from "react";

import { api, type HermesRecommendation } from "../api/client";

export function HermesPage() {
  const queryClient = useQueryClient();
  const [task, setTask] = useState("Run a debate review for this architecture.");
  const [lesson, setLesson] = useState("Use group chat when debate review is required.");
  const [recommendation, setRecommendation] = useState<HermesRecommendation | null>(null);
  const insights = useQuery({
    queryKey: ["hermes"],
    queryFn: () => api.hermesInsights(),
  });
  const recommend = useMutation({
    mutationFn: () =>
      api.recommendWithHermes({
        task,
        mode_candidates: ["dispatch", "group_chat"],
        model_candidates: ["deepseek-chat", "gpt-4o"],
        skill_candidates: ["architecture-review", "safe-shell"],
      }),
    onSuccess: setRecommendation,
  });
  const feedback = useMutation({
    mutationFn: () =>
      api.recordHermesFeedback({
        outcome: "success",
        lesson,
        tags: ["debate", "review"],
        weight: 5,
      }),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["hermes"] }),
  });

  function submitRecommendation(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    recommend.mutate();
  }

  function submitFeedback(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    feedback.mutate();
  }

  if (insights.isLoading) return <p>Loading Hermes...</p>;
  if (insights.isError) return <p role="alert">Failed to load Hermes</p>;
  return (
    <section>
      <h2>Hermes learning</h2>
      <p>
        Hermes stores safe experience patterns for routing, model choice, skill choice, and failure
        avoidance. It recommends; it does not bypass approvals.
      </p>
      <form onSubmit={submitRecommendation}>
        <label>
          Task to analyze
          <textarea
            aria-label="Task to analyze"
            value={task}
            onChange={(event) => setTask(event.currentTarget.value)}
          />
        </label>
        <button type="submit">Ask Hermes</button>
      </form>
      {recommendation && (
        <article>
          <h3>Recommendation</h3>
          <p>Mode: {recommendation.recommended_mode}</p>
          <p>Model: {recommendation.recommended_model ?? "default"}</p>
          <p>Skills: {recommendation.recommended_skills.join(", ") || "none"}</p>
          <p>Confidence: {Math.round(recommendation.confidence * 100)}%</p>
          <p>Requires approval: {recommendation.requires_approval ? "yes" : "no"}</p>
          <ul>
            {recommendation.reasons.map((reason) => (
              <li key={reason}>{reason}</li>
            ))}
          </ul>
        </article>
      )}
      <form onSubmit={submitFeedback}>
        <label>
          Safe lesson
          <textarea
            aria-label="Safe lesson"
            value={lesson}
            onChange={(event) => setLesson(event.currentTarget.value)}
          />
        </label>
        <button type="submit">Record feedback</button>
      </form>
      <h3>Experience memory</h3>
      {insights.data?.map((insight) => (
        <article key={insight.id}>
          <h4>{insight.outcome}</h4>
          <p>{insight.lesson}</p>
          <p>Tags: {insight.tags.join(", ")}</p>
          <p>Weight: {insight.weight}</p>
        </article>
      ))}
    </section>
  );
}
