/* duty.js — 现场分工：整批重排 / 从断点续跑；按钮状态与错误就地反馈 */
const dutyState = { busy: false };

function dutyMessage(text, bad) {
  const node = document.getElementById("duty-msg");
  if (!node) {
    return;
  }
  node.textContent = text;
  node.classList.toggle("err", Boolean(bad));
}

async function dutyPost(body) {
  const response = await fetch("/api/duty", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const text = await response.text();
  let data = null;
  try {
    data = JSON.parse(text);
  } catch (error) {
    data = { error: "E_PARSE", message: text.slice(0, 120) };
  }
  if (!response.ok) {
    throw new Error(data.message || "请求失败 " + response.status);
  }
  return data;
}

function currentDutyId() {
  const node = document.querySelector("[data-duty-id]");
  return node ? node.dataset.dutyId : "";
}

async function dutyRun(resume) {
  if (dutyState.busy) {
    return;
  }
  dutyState.busy = true;
  const buttons = [document.getElementById("duty-run"),
    document.getElementById("duty-resume")];
  buttons.forEach((node) => {
    if (node) {
      node.disabled = true;
    }
  });
  dutyMessage("运行中", false);
  try {
    const previous = currentDutyId();
    const body = resume && previous
      ? { mode: "resume", resume_of: previous }
      : { mode: "new_version" };
    const data = await dutyPost(body);
    dutyMessage(
      "分工 " + data.duty_id + " · v" + data.version +
        " · 预约 " + data.summary.appointments +
        " · 待接管 " + data.summary.takeover_pending,
      false
    );
    window.location.href = "/duty?duty_id=" + encodeURIComponent(data.duty_id);
  } catch (error) {
    dutyMessage(error.message, true);
  } finally {
    dutyState.busy = false;
    buttons.forEach((node) => {
      if (node) {
        node.disabled = false;
      }
    });
  }
}

function dutyBind() {
  const runButton = document.getElementById("duty-run");
  const resumeButton = document.getElementById("duty-resume");
  if (runButton) {
    runButton.addEventListener("click", () => dutyRun(false));
  }
  if (resumeButton) {
    resumeButton.addEventListener("click", () => dutyRun(true));
  }
}

document.addEventListener("DOMContentLoaded", dutyBind);
