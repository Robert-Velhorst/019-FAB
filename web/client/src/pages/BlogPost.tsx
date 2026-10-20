import { lazy, Suspense } from "react";
import { motion } from "framer-motion";
import { Calendar, Clock, ArrowLeft, BookOpen } from "lucide-react";
import { Button } from "@/components/ui/button";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { Link, useParams } from "wouter";
import { useLanguage } from "@/contexts/LanguageContext";
import { trpc } from "@/lib/trpc";

const Streamdown = lazy(async () => {
  const { Streamdown: MarkdownRenderer } = await import("streamdown");
  return { default: MarkdownRenderer };
});

const fadeUp = {
  hidden: { opacity: 0, y: 30 },
  visible: { opacity: 1, y: 0, transition: { duration: 0.7, ease: [0.25, 0.1, 0.25, 1] as const } },
};

const stagger = {
  visible: { transition: { staggerChildren: 0.12 } },
};

const categoryColors: Record<string, string> = {
  update: "bg-teal/10 text-teal",
  milestone: "bg-sage-light text-sage",
  feature: "bg-sand text-charcoal",
  announcement: "bg-teal text-white",
};

export default function BlogPost() {
  const { t, lang: language } = useLanguage();
  const params = useParams<{ slug: string }>();
  const slug = params.slug || "";

  const { data: post, isLoading } = trpc.blog.bySlug.useQuery(
    { slug },
    { enabled: !!slug }
  );

  const formatDate = (date: Date | string | null) => {
    if (!date) return "";
    const d = new Date(date);
    return d.toLocaleDateString(language === "nl" ? "nl-NL" : "en-US", {
      year: "numeric",
      month: "long",
      day: "numeric",
    });
  };

  if (isLoading) {
    return (
      <div className="min-h-screen bg-warm-white">
        <Navbar />
        <div className="container pt-32 pb-20">
          <div className="max-w-3xl mx-auto animate-pulse">
            <div className="h-4 bg-sand/60 rounded w-1/4 mb-6" />
            <div className="h-10 bg-sand/60 rounded w-3/4 mb-4" />
            <div className="h-4 bg-sand/60 rounded w-1/2 mb-8" />
            <div className="h-64 bg-sand/60 rounded-2xl mb-8" />
            <div className="space-y-3">
              <div className="h-4 bg-sand/60 rounded w-full" />
              <div className="h-4 bg-sand/60 rounded w-5/6" />
              <div className="h-4 bg-sand/60 rounded w-full" />
              <div className="h-4 bg-sand/60 rounded w-4/5" />
            </div>
          </div>
        </div>
        <Footer />
      </div>
    );
  }

  if (!post) {
    return (
      <div className="min-h-screen bg-warm-white">
        <Navbar />
        <div className="container pt-32 pb-20">
          <div className="max-w-md mx-auto text-center">
            <BookOpen className="w-16 h-16 text-sand-dark/40 mx-auto mb-4" />
            <h1 className="text-2xl text-charcoal mb-4 font-semibold">
              {t("blog.postNotFound")}
            </h1>
            <p className="text-charcoal-light mb-8">
              {t("blog.postNotFoundDesc")}
            </p>
            <Link href="/blog">
              <Button className="bg-teal hover:bg-teal-light text-white rounded-xl">
                <ArrowLeft className="w-4 h-4 mr-2" />
                {t("blog.backToBlog")}
              </Button>
            </Link>
          </div>
        </div>
        <Footer />
      </div>
    );
  }

  const title = language === "nl" && post.titleNl ? post.titleNl : post.title;
  const rawContent = language === "nl" && post.contentNl ? post.contentNl : post.content;

  // Strip the first H1 from content if it duplicates the page title
  const content = rawContent.replace(/^#\s+.+\n+/, "");

  return (
    <div className="min-h-screen bg-warm-white">
      <Navbar />

      <article className="pt-24 pb-20 lg:pt-32 lg:pb-28">
        <div className="container">
          <motion.div
            className="max-w-3xl mx-auto"
            initial="hidden"
            animate="visible"
            variants={stagger}
          >
            {/* Back link */}
            <motion.div variants={fadeUp} className="mb-8">
              <Link href="/blog">
                <Button variant="ghost" className="text-charcoal-light hover:text-teal -ml-4">
                  <ArrowLeft className="w-4 h-4 mr-2" />
                  {t("blog.backToBlog")}
                </Button>
              </Link>
            </motion.div>

            {/* Meta */}
            <motion.div variants={fadeUp} className="flex items-center gap-3 mb-4">
              <span className={`px-3 py-1 rounded-full text-xs font-medium ${categoryColors[post.category] || "bg-sand text-charcoal"}`}>
                {t(`blog.cat${post.category.charAt(0).toUpperCase() + post.category.slice(1)}`)}
              </span>
              <span className="flex items-center gap-1.5 text-sm text-charcoal-light">
                <Calendar className="w-3.5 h-3.5" />
                {formatDate(post.publishedAt)}
              </span>
              <span className="flex items-center gap-1.5 text-sm text-charcoal-light">
                <Clock className="w-3.5 h-3.5" />
                {post.readTimeMinutes} min {t("blog.readTime")}
              </span>
            </motion.div>

            {/* Title */}
            <motion.h1
              variants={fadeUp}
              className="text-3xl sm:text-4xl lg:text-5xl leading-[1.1] tracking-tight text-charcoal mb-8"
            >
              {title}
            </motion.h1>

            {/* Cover Image */}
            {post.coverImage && (
              <motion.div variants={fadeUp} className="mb-10">
                <img
                  src={post.coverImage}
                  alt={title}
                  className="w-full rounded-2xl shadow-lg"
                />
              </motion.div>
            )}

            {/* Content */}
            <motion.div
              variants={fadeUp}
              className="prose prose-lg max-w-none prose-headings:text-charcoal prose-headings:font-sans prose-p:text-charcoal-light prose-p:leading-relaxed prose-a:text-teal prose-a:no-underline hover:prose-a:underline prose-strong:text-charcoal prose-blockquote:border-teal prose-blockquote:text-charcoal-light"
            >
              <Suspense fallback={(
                <div role="status" aria-live="polite" className="min-h-64 space-y-3 animate-pulse">
                  <span className="sr-only">{language === "nl" ? "Artikelinhoud laden" : "Loading article content"}</span>
                  <div aria-hidden="true" className="h-4 w-full rounded bg-sand/60" />
                  <div aria-hidden="true" className="h-4 w-5/6 rounded bg-sand/60" />
                  <div aria-hidden="true" className="h-4 w-4/5 rounded bg-sand/60" />
                  <div aria-hidden="true" className="h-4 w-full rounded bg-sand/60" />
                </div>
              )}>
                <Streamdown>{content}</Streamdown>
              </Suspense>
            </motion.div>

            {/* Bottom CTA */}
            <motion.div
              variants={fadeUp}
              className="mt-16 p-8 bg-sand/40 rounded-2xl text-center"
            >
              <h3 className="text-xl text-charcoal mb-3 font-semibold">
                {t("blog.ctaTitle")}
              </h3>
              <p className="text-charcoal-light mb-6">
                {t("blog.ctaDesc")}
              </p>
              <Link href="/blog">
                <Button className="bg-teal hover:bg-teal-light text-white rounded-xl px-8">
                  {t("blog.ctaButton")}
                  <ArrowLeft className="w-4 h-4 ml-2 rotate-180" />
                </Button>
              </Link>
            </motion.div>
          </motion.div>
        </div>
      </article>

      <Footer />
    </div>
  );
}
