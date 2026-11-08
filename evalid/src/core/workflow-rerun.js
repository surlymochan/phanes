import { existsSync, readFileSync, rmSync, writeFileSync, mkdirSync } from 'node:fs';
import { dirname, resolve } from 'node:path';

function nowIso() {
  return new Date().toISOString();
}

function loadExecutorState(stateFile) {
  if (!existsSync(stateFile)) {
    return null;
  }
  try {
    const raw = readFileSync(stateFile, 'utf8');
    return raw.trim() ? JSON.parse(raw) : null;
  } catch {
    return null;
  }
}

function saveExecutorState(stateFile, state) {
  mkdirSync(dirname(stateFile), { recursive: true });
  writeFileSync(stateFile, `${JSON.stringify({ ...state, updated_at: nowIso() }, null, 2)}\n`, 'utf8');
}

function trimCompletedPhases(completedPhases = [], allowedPhaseIds = new Set()) {
  return (Array.isArray(completedPhases) ? completedPhases : []).filter((entry) => {
    if (typeof entry === 'string') {
      return allowedPhaseIds.has(entry);
    }
    if (entry && typeof entry === 'object') {
      return allowedPhaseIds.has(entry.phase);
    }
    return false;
  });
}

export function prepareWorkflowRerun({
  workflow,
  phases,
  projectRoot = process.cwd(),
  changeId = null,
  phaseId,
  atomId = null,
}) {
  if (!workflow || typeof workflow !== 'object' || typeof workflow.name !== 'string' || !workflow.name.trim()) {
    throw new Error('Invalid workflow: workflow name is required');
  }
  if (!Array.isArray(phases)) {
    throw new Error('Invalid workflow phases: phases must be an array');
  }

  const phaseIds = new Set();
  for (const [index, phase] of phases.entries()) {
    if (!phase || typeof phase !== 'object' || typeof phase.id !== 'string' || !phase.id.trim()) {
      throw new Error(`Invalid workflow phase at index ${index}: phase id is required`);
    }
    if (phaseIds.has(phase.id)) {
      throw new Error(`Invalid workflow phases: duplicate phase id "${phase.id}"`);
    }
    phaseIds.add(phase.id);
    if (!Array.isArray(phase.atoms)) {
      throw new Error(`Invalid workflow phase "${phase.id}": atoms must be an array`);
    }
  }

  const targetPhase = phases.find((phase) => phase.id === phaseId);
  if (!targetPhase) {
    throw new Error(`Unknown rerun phase: ${phaseId}`);
  }

  if (atomId && !(targetPhase.atoms || []).some((atomRef) => atomRef.id === atomId)) {
    throw new Error(`Atom "${atomId}" is not present in phase "${phaseId}"`);
  }

  const phaseOrder = phases.map((phase) => phase.id);
  const phaseIndex = phaseOrder.indexOf(phaseId);
  const previousPhase = phaseIndex > 0 ? phaseOrder[phaseIndex - 1] : null;
  const allowedCompleted = new Set(previousPhase ? phaseOrder.slice(0, phaseIndex) : []);
  const stateFile = resolve(projectRoot, '.as-xflow', 'workflow-state.json');
  const current = loadExecutorState(stateFile) || {};
  const nextState = {
    workflow: workflow.name,
    change_id: current.change_id || changeId || null,
    started_at: current.started_at || nowIso(),
    status: 'active',
    completed_phases: trimCompletedPhases(current.completed_phases, allowedCompleted),
    last_completed_phase: previousPhase,
    rerun: {
      phase_id: phaseId,
      atom_id: atomId || null,
      requested_at: nowIso(),
    },
  };

  saveExecutorState(stateFile, nextState);

  const pendingGateFile = resolve(projectRoot, '.as-xflow', 'pending-gates', `${phaseId}.json`);
  rmSync(pendingGateFile, { force: true });

  return {
    state_file: stateFile,
    state: nextState,
    rerun: nextState.rerun,
  };
}
