import { Link } from "@tanstack/solid-router";

export function AgenticAIDefinition() {
  return (
    <>
      <p>
        Agentic AI means AI that can work toward a goal through several steps. It can use a tool, look at the result,
        and decide what to do next. A chatbot often feels different: you ask a question, get an answer, and decide what
        to ask next yourself.
      </p>
      <p>
        The easiest way to see the difference is to compare a one-off chat with an AI agent in OpenBot. OpenBot lets you
        create a named agent, give it instructions, and assign work that may involve a browser, files, or another agent.
      </p>

      <h2>The Same Request, Two Ways of Working</h2>
      <p>Suppose you need a short article about your latest product update.</p>
      <p>
        In a typical chatbot conversation, you paste a few links and ask for a summary. Then you ask it to turn the
        summary into a draft. If a claim looks doubtful, you ask another question, find the source, and move the text
        into your document. Next week you may need to explain the job again.
      </p>
      <p>
        In OpenBot, you could give a research agent one clear assignment: “Read these release notes, check each
        important claim against its source, and save a short brief with links. Flag anything you cannot confirm.” The
        agent can open pages in OpenBot&apos;s built-in browser and work with files. You can then pass the brief to a
        writing agent, or ask the same agent to draft the article.
      </p>
      <p>
        You still read the result and decide whether it is ready to publish. The difference is that the agent can carry
        out more of the steps between your request and that review.
      </p>

      <h2>What Makes the OpenBot Agent Useful?</h2>
      <p>
        <strong>It keeps a place for ongoing work.</strong> Each OpenBot agent has its own instructions, conversation,
        and workspace. If you return to a task later, the conversation and files remain on the computer running OpenBot.
        You do not have to recreate the same agent every time.
      </p>
      <p>
        <strong>It can leave you a real deliverable.</strong> An answer in chat can be helpful, but a saved brief or
        draft is easier to review, edit, and pass along. An OpenBot agent can use files and a built-in browser as part
        of the job.
      </p>
      <p>
        <strong>It can work with another agent.</strong> A research agent can share its notes with a writing agent. A
        reviewer can check the draft before it reaches you. OpenBot supports messages, file sharing, and task handoffs
        between agents. Start with one agent; add another when the roles are genuinely different.
      </p>
      <p>
        <strong>You can choose the AI service.</strong> OpenBot can run agents with providers including Codex, Claude
        Code, Gemini, and Grok. OpenBot organizes the work; the connected AI service helps the agent carry it out.
      </p>
      <p>
        These are practical advantages for a repeated, multi-step job. They do not make every agent answer correct. A
        missing source should be marked as missing, not turned into a confident sentence.
      </p>

      <h2>Is a Chatbot Always Less Capable?</h2>
      <p>
        No. “Chatbot” describes how you talk to a product. Some chatbots also browse, use tools, remember information,
        or run agents. The useful comparison is between a simple, one-off conversation and an agent you can give an
        ongoing job.
      </p>
      <p>
        For a quick definition, a rough idea, or a short rewrite, chat may be all you need. For research, drafting,
        checking sources, and handing work between people or agents, OpenBot gives the job a place to continue. For a
        broader introduction, read{" "}
        <Link to="/guides/$slug" params={{ slug: "what-are-ai-agents" }}>
          What Are AI Agents?
        </Link>
      </p>

      <h2>Try a Small Task First</h2>
      <p>
        Create one OpenBot agent for a narrow job. For example: “Check these three approved pages and save a one-page
        brief with links. Tell me when a fact cannot be verified. Do not publish anything.” Read the brief, check its
        sources, and adjust the instructions before giving the agent a larger task.
      </p>
      <p>
        OpenBot agents can read and change files and use the network, so give them tasks you trust and review important
        results. The AI provider you connect may receive the prompts sent to it, even though OpenBot keeps the workspace
        and files on the computer running the app.
      </p>

      <h2>Frequently Asked Questions</h2>
      <h3>What Is an AI Agent in OpenBot?</h3>
      <p>
        A saved AI teammate with instructions and its own place for conversations and files. You can give it a task and
        review what it produces.
      </p>
      <h3>Does Agentic AI Mean Publishing Without Approval?</h3>
      <p>No. The agent can do work between steps, while you decide whether a draft is ready to publish.</p>
      <h3>Do I Need Several Agents?</h3>
      <p>
        No. One agent can research and draft. Add a second when a separate review or writing role makes the result
        easier to manage.
      </p>
    </>
  );
}
