const form = document.querySelector("#connection");
const { source, destination, direction } = form.elements;
const hint = document.querySelector("#permission-hint");

function updateChoices() {
  const readOnly = source.selectedOptions[0]?.dataset.writable === "no";
  for (const option of destination.options) {
    option.disabled = Boolean(source.value) && option.value === source.value;
  }
  if (destination.selectedOptions[0]?.disabled) destination.value = "";
  const both = direction.querySelector('[value="both"]');
  both.disabled = readOnly;
  if (readOnly) direction.value = "one";
  const reverse = form.querySelector("[data-reverse-label]");
  reverse.hidden = direction.value !== "both";
  reverse.querySelector("input").disabled = reverse.hidden;
  hint.textContent = readOnly
    ? "This source is read-only. Choose a writable source to enable two-way sync."
    : "Two-way sync requires write access on both calendars.";
}
direction.addEventListener("change", updateChoices);
source.addEventListener("change", updateChoices);
window.addEventListener("pageshow", updateChoices);
updateChoices();
