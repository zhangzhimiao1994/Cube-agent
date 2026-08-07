import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState } from "react";

import { api } from "../api/client";

export function SkillsPage() {
  const [filename, setFilename] = useState("");
  const queryClient = useQueryClient();
  const skills = useQuery({ queryKey: ["skills"], queryFn: () => api.skills() });
  const upload = useMutation({
    mutationFn: () => api.uploadSkill(filename),
    onSuccess: () => {
      setFilename("");
      void queryClient.invalidateQueries({ queryKey: ["skills"] });
    },
  });
  const approve = useMutation({
    mutationFn: (id: string) => api.approveSkill(id),
    onSuccess: () => void queryClient.invalidateQueries({ queryKey: ["skills"] }),
  });

  if (skills.isLoading) return <p>Loading skills...</p>;
  if (skills.isError) return <p role="alert">Failed to load skills</p>;
  return (
    <section>
      <h2>Skills governance</h2>
      <label>
        Skill ZIP
        <input
          aria-label="Skill ZIP"
          type="file"
          accept=".zip"
          onChange={(event) => setFilename(event.currentTarget.files?.[0]?.name ?? "")}
        />
      </label>
      <button type="button" disabled={!filename} onClick={() => upload.mutate()}>
        Upload
      </button>
      {skills.data?.map((skill) => (
        <article key={skill.id}>
          <h3>{skill.name}</h3>
          <p>Status: {skill.status}</p>
          <p>Scan Diff: {skill.scan_diff.join("; ")}</p>
          <p>Requested permissions: {skill.requested_permissions.join(", ")}</p>
          <button type="button" onClick={() => approve.mutate(skill.id)}>
            Approve and enable
          </button>
        </article>
      ))}
    </section>
  );
}
