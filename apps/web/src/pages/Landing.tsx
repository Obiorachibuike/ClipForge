import { Link } from 'react-router-dom';
import { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { AnimatePresence, motion } from 'framer-motion';
import {
  ArrowRight,
  Captions,
  Check,
  ChevronDown,
  Crop,
  Layers,
  Play,
  Scan,
  Scissors,
  Sparkles,
  Upload,
  Wand2,
} from 'lucide-react';
import { api } from '@/lib/api';
import ClipForgeMark from '@/components/ClipForgeMark';
import ThemeToggle from '@/components/ThemeToggle';
import { queryKeys } from '@/lib/query';
import { useAuthStore } from '@/stores/auth';
import { cn, formatBytes } from '@/lib/format';
import type { Plan } from '@/types/api';

const STEPS = [
  {
    icon: Upload,
    title: 'Upload the long video',
    body: 'Chunked and resumable, so a two-hour recording survives a dropped connection. Nothing is buffered in the browser.',
  },
  {
    icon: Scan,
    title: 'ClipForge reads it',
    body: 'Word-level transcription, pause and energy analysis, face tracking. Every number comes from your actual footage.',
  },
  {
    icon: Wand2,
    title: 'Moments are scored',
    body: 'Hooks, self-contained ideas, payoff structure and delivery dynamics produce ranked candidates you can explain.',
  },
  {
    icon: Layers,
    title: 'Review, edit, render',
    body: 'Keep what works, trim what does not, style captions, then render vertical, square or landscape masters.',
  },
];

const FEATURES = [
  {
    icon: Scissors,
    title: 'AI clip discovery',
    body: 'Candidates carry a start, end, hook, reason and a transparent score breakdown across nine measured signals — never a black box.',
  },
  {
    icon: Crop,
    title: 'Smart framing',
    body: 'Crop windows follow the active speaker with dead-zone smoothing and velocity limits, so pans are deliberate and heads stay in frame.',
  },
  {
    icon: Captions,
    title: 'Word-level captions',
    body: 'Karaoke, Bold, Classic, Minimal, Podcast, Creator and High Contrast presets. Rewrite any word without breaking its timing.',
  },
  {
    icon: Play,
    title: 'Built for review',
    body: 'A player with click-word-to-seek, timeline boundaries, per-candidate jumps and a keyboard-driven review flow.',
  },
];

const PLATFORMS = [
  { name: 'TikTok', ratio: '9:16 · 1080×1920' },
  { name: 'YouTube Shorts', ratio: '9:16 · 1080×1920' },
  { name: 'Instagram Reels', ratio: '9:16 · 1080×1920' },
  { name: 'Facebook Reels', ratio: '9:16 · 1080×1920' },
  { name: 'Square posts', ratio: '1:1 · 1080×1080' },
  { name: 'Landscape', ratio: '16:9 · 1920×1080' },
];

const FAQ = [
  {
    q: 'How are clip scores calculated?',
    a: 'Nine signals are measured from your transcript and audio — hook strength, surprise, emotional charge, insight density, story structure, self-containment, delivery dynamics, transcript confidence and length fit. The weights are visible, and each candidate shows the breakdown that produced its score.',
  },
  {
    q: 'What happens to my video?',
    a: 'In ClipForge Cloud mode the file is uploaded to this deployment and processed by its workers. In Private AI mode, transcription and language tasks run against the AI providers you configure. Either way the video file reaches this server — we never claim fully local processing on a cloud deployment.',
  },
  {
    q: 'Can I fix the captions?',
    a: 'Yes. Every word is editable and timing is preserved when you rewrite text. You can also restyle position, size, weight, colour, stroke, shadow, highlight, animation and line limits, or save your own preset.',
  },
  {
    q: 'Do I need to install anything?',
    a: 'No. ClipForge is a browser application — no desktop runtime. The same account works on a laptop and a phone; the editor is designed for desktop and tablet, while review works on mobile.',
  },
  {
    q: 'What if the transcription is wrong?',
    a: 'You can correct words in the transcript tab, re-run transcription with a different provider, or paste the spoken script to have ClipForge align it to your audio for exact word timings.',
  },
];

function Hero() {
  const status = useAuthStore((state) => state.status);
  return (
    <section className="relative overflow-hidden border-b border-ink-800">
      <div className="pointer-events-none absolute inset-0 bg-hero-glow" />
      <div className="pointer-events-none absolute inset-0 bg-grid-dark bg-grid opacity-[0.35]" />
      <div className="relative mx-auto max-w-6xl px-6 py-20 sm:py-28">
        <motion.div
          initial={{ opacity: 0, y: 18 }}
          animate={{ opacity: 1, y: 0 }}
          transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
          className="max-w-3xl"
        >
          <span className="badge-accent">
            <Sparkles className="h-3 w-3" aria-hidden />
            Word-level AI clipping
          </span>
          <h1 className="mt-6 text-4xl font-semibold leading-[1.05] tracking-tight text-slate-100 sm:text-6xl">
            Turn long videos into short-form content{' '}
            <span className="text-gradient-accent">automatically.</span>
          </h1>
          <p className="mt-6 max-w-2xl text-lg leading-relaxed text-slate-400">
            ClipForge transcribes your recording, finds the moments worth posting, tracks the speaker for vertical
            framing, adds word-level captions and renders platform-ready clips — all in the browser.
          </p>
          <div className="mt-9 flex flex-wrap items-center gap-3">
            <Link to={status === 'authenticated' ? '/dashboard' : '/register'} className="btn-primary px-6 py-3">
              Start Creating
              <ArrowRight className="h-4 w-4" aria-hidden />
            </Link>
            <Link
              to={status === 'authenticated' ? '/dashboard' : '/login'}
              className="btn-secondary px-6 py-3"
            >
              Try ClipForge
            </Link>
          </div>
          <p className="mt-5 text-xs text-slate-500">
            No desktop app. No credit card to try the pipeline on a short video.
          </p>
        </motion.div>
      </div>
    </section>
  );
}

function HowItWorks() {
  return (
    <section className="border-b border-ink-800 bg-ink-900/40 py-20">
      <div className="mx-auto max-w-6xl px-6">
        <h2 className="text-3xl font-semibold tracking-tight text-slate-100">How it works</h2>
        <p className="mt-3 max-w-2xl text-slate-400">
          Four steps, each one a real processing job you can watch and cancel — no simulated progress bars.
        </p>
        <div className="mt-12 grid gap-6 sm:grid-cols-2 lg:grid-cols-4">
          {STEPS.map((step, index) => (
            <motion.div
              key={step.title}
              initial={{ opacity: 0, y: 20 }}
              whileInView={{ opacity: 1, y: 0 }}
              viewport={{ once: true, margin: '-80px' }}
              transition={{ duration: 0.45, delay: index * 0.08 }}
              className="card card-hover p-6"
            >
              <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-accent-500/15 text-accent-300">
                <step.icon className="h-5 w-5" aria-hidden />
              </span>
              <p className="mt-4 text-2xs font-semibold uppercase tracking-widest text-slate-500">
                Step {index + 1}
              </p>
              <h3 className="mt-1 text-base font-semibold text-slate-100">{step.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-400">{step.body}</p>
            </motion.div>
          ))}
        </div>
      </div>
    </section>
  );
}

function FeatureGrid() {
  return (
    <section id="features" className="border-b border-ink-800 py-20">
      <div className="mx-auto max-w-6xl px-6">
        <h2 className="text-3xl font-semibold tracking-tight text-slate-100">
          Everything between raw footage and a post
        </h2>
        <div className="mt-12 grid gap-6 md:grid-cols-2">
          {FEATURES.map((feature) => (
            <div key={feature.title} className="card card-hover p-6">
              <span className="flex h-10 w-10 items-center justify-center rounded-xl bg-violet-500/15 text-violet-400">
                <feature.icon className="h-5 w-5" aria-hidden />
              </span>
              <h3 className="mt-4 text-lg font-semibold text-slate-100">{feature.title}</h3>
              <p className="mt-2 text-sm leading-relaxed text-slate-400">{feature.body}</p>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Platforms() {
  return (
    <section className="border-b border-ink-800 bg-ink-900/40 py-20">
      <div className="mx-auto max-w-6xl px-6">
        <h2 className="text-3xl font-semibold tracking-tight text-slate-100">Rendered for the platform</h2>
        <p className="mt-3 max-w-2xl text-slate-400">
          Export presets produce H.264/AAC MP4s at the dimensions each platform wants.
        </p>
        <div className="mt-10 grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
          {PLATFORMS.map((platform) => (
            <div
              key={platform.name}
              className="flex items-center justify-between rounded-xl border border-ink-700/70 bg-ink-900/60 px-4 py-3"
            >
              <span className="text-sm font-medium text-slate-200">{platform.name}</span>
              <span className="font-mono text-2xs text-slate-500">{platform.ratio}</span>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Pricing() {
  // Real plan data from the API: prices and limits are never hard-coded here.
  const { data } = useQuery({
    queryKey: queryKeys.plans,
    queryFn: () => api.get<{ plans: Plan[] }>('/billing/plans'),
    staleTime: 10 * 60_000,
  });

  const plans = data?.plans ?? [];

  return (
    <section id="pricing" className="border-b border-ink-800 py-20">
      <div className="mx-auto max-w-6xl px-6">
        <h2 className="text-3xl font-semibold tracking-tight text-slate-100">Pricing</h2>
        <p className="mt-3 max-w-2xl text-slate-400">
          Start free. Upgrade when the pipeline has earned it.
        </p>
        {plans.length === 0 ? (
          <div className="mt-10 grid gap-6 md:grid-cols-3">
            {[0, 1, 2].map((index) => (
              <div key={index} className="card h-72 animate-pulse bg-ink-850/60" />
            ))}
          </div>
        ) : (
          <div className="mt-10 grid gap-6 md:grid-cols-3">
            {plans.map((plan) => (
              <div
                key={plan.key}
                className={cn(
                  'card flex flex-col p-6',
                  plan.highlighted && 'border-accent-500/40 shadow-glow',
                )}
              >
                {plan.highlighted ? <span className="badge-accent mb-3 w-fit">Most popular</span> : null}
                <h3 className="text-lg font-semibold text-slate-100">{plan.name}</h3>
                <p className="mt-1 text-sm text-slate-400">{plan.tagline}</p>
                <p className="mt-5 text-3xl font-semibold text-slate-100">
                  {plan.price_minor === 0 ? 'Free' : `$${(plan.price_minor / 100).toFixed(0)}`}
                  {plan.price_minor > 0 ? (
                    <span className="text-sm font-normal text-slate-500">
                      {plan.interval === 'once' ? ' once' : ' / month'}
                    </span>
                  ) : null}
                </p>
                <ul className="mt-6 flex-1 space-y-2.5">
                  {plan.features.slice(0, 6).map((feature) => (
                    <li key={feature} className="flex gap-2.5 text-sm text-slate-300">
                      <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" aria-hidden />
                      {feature}
                    </li>
                  ))}
                  <li className="flex gap-2.5 text-sm text-slate-400">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" aria-hidden />
                    {Math.round(plan.limits.minutes_processed)} minutes processed
                  </li>
                  <li className="flex gap-2.5 text-sm text-slate-400">
                    <Check className="mt-0.5 h-4 w-4 shrink-0 text-emerald-400" aria-hidden />
                    {formatBytes(plan.limits.storage_bytes)} storage
                  </li>
                </ul>
                <Link
                  to="/register"
                  className={cn('mt-6', plan.highlighted ? 'btn-primary' : 'btn-secondary')}
                >
                  {plan.price_minor === 0 ? 'Start free' : `Choose ${plan.name}`}
                </Link>
              </div>
            ))}
          </div>
        )}
      </div>
    </section>
  );
}

function Faq() {
  const [open, setOpen] = useState<number | null>(0);
  return (
    <section className="border-b border-ink-800 bg-ink-900/40 py-20">
      <div className="mx-auto max-w-3xl px-6">
        <h2 className="text-3xl font-semibold tracking-tight text-slate-100">Questions</h2>
        <div className="mt-8 divide-y divide-ink-700">
          {FAQ.map((item, index) => (
            <div key={item.q}>
              <button
                type="button"
                className="flex w-full items-center justify-between gap-4 py-5 text-left"
                onClick={() => setOpen(open === index ? null : index)}
                aria-expanded={open === index}
              >
                <span className="text-base font-medium text-slate-100">{item.q}</span>
                <ChevronDown
                  className={cn('h-4 w-4 shrink-0 text-slate-500 transition-transform', open === index && 'rotate-180')}
                  aria-hidden
                />
              </button>
              <AnimatePresence initial={false}>
                {open === index ? (
                  <motion.div
                    initial={{ height: 0, opacity: 0 }}
                    animate={{ height: 'auto', opacity: 1 }}
                    exit={{ height: 0, opacity: 0 }}
                    transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
                    className="overflow-hidden"
                  >
                    <p className="pb-5 text-sm leading-relaxed text-slate-400">{item.a}</p>
                  </motion.div>
                ) : null}
              </AnimatePresence>
            </div>
          ))}
        </div>
      </div>
    </section>
  );
}

function Footer() {
  return (
    <footer className="py-12">
      <div className="mx-auto flex max-w-6xl flex-col items-center justify-between gap-4 px-6 sm:flex-row">
        <div className="flex items-center gap-2.5">
          <span className="flex h-7 w-7 items-center justify-center rounded-lg bg-gradient-to-br from-accent-500 to-violet-500 shadow-[0_6px_18px_rgba(91,108,255,0.28)]">
            <ClipForgeMark className="h-4 w-4 text-white" />
          </span>
          <span className="text-sm font-semibold text-slate-100">ClipForge</span>
        </div>
        <div className="flex items-center gap-6 text-sm text-slate-500">
          <Link to="/pricing" className="hover:text-slate-300">
            Pricing
          </Link>
          <Link to="/register" className="hover:text-slate-300">
            Start Creating
          </Link>
        </div>
      </div>
    </footer>
  );
}

export default function Landing() {
  return (
    <div className="min-h-screen bg-ink-950">
      <header className="sticky top-0 z-40 border-b border-ink-800/80 bg-ink-950/80 backdrop-blur-md">
        <div className="mx-auto flex h-16 max-w-6xl items-center justify-between px-6">
          <Link to="/" className="flex items-center gap-2.5">
            <span className="flex h-8 w-8 items-center justify-center rounded-lg bg-gradient-to-br from-accent-500 to-violet-500 shadow-[0_8px_24px_rgba(91,108,255,0.3)]">
              <ClipForgeMark className="h-5 w-5 text-white" />
            </span>
            <span className="text-base font-semibold tracking-tight text-slate-100">ClipForge</span>
          </Link>
          <nav className="hidden items-center gap-6 text-sm text-slate-400 md:flex">
            <a href="#features" className="hover:text-slate-200">
              Features
            </a>
            <a href="#pricing" className="hover:text-slate-200">
              Pricing
            </a>
          </nav>
          <div className="flex items-center gap-2">
            <ThemeToggle className="mr-1" />
            <Link to="/login" className="btn-ghost btn-sm">
              Sign in
            </Link>
            <Link to="/register" className="btn-primary btn-sm">
              Start Creating
            </Link>
          </div>
        </div>
      </header>
      <Hero />
      <HowItWorks />
      <FeatureGrid />
      <Platforms />
      <Pricing />
      <Faq />
      <Footer />
    </div>
  );
}
