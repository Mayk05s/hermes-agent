export async function writeClipboardText(text: string): Promise<void> {
  const modernClipboard = navigator.clipboard?.writeText;
  if (modernClipboard) {
    try {
      await modernClipboard.call(navigator.clipboard, text);
      return;
    } catch {
      // Fall back for non-secure origins or browsers that drop transient click activation.
    }
  }

  const textarea = document.createElement("textarea");
  textarea.value = text;
  textarea.setAttribute("readonly", "true");
  textarea.style.position = "fixed";
  textarea.style.left = "-9999px";
  textarea.style.top = "0";
  document.body.appendChild(textarea);
  textarea.focus();
  textarea.select();
  try {
    const copied = document.execCommand("copy");
    if (!copied) {
      throw new Error("Browser refused clipboard write");
    }
  } finally {
    document.body.removeChild(textarea);
  }
}
