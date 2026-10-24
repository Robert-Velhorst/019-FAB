import { useEffect, useState } from "react";
import { motion } from "framer-motion";
import { CheckCircle, ArrowRight, Loader2 } from "lucide-react";
import { Button } from "@/components/ui/button";
import Navbar from "@/components/Navbar";
import Footer from "@/components/Footer";
import { Link, useSearch } from "wouter";
import { useLanguage } from "@/contexts/LanguageContext";
import { trpc } from "@/lib/trpc";

const fadeUp = {
  hidden: { opacity: 0, y: 30 },
  visible: {
    opacity: 1,
    y: 0,
    transition: { duration: 0.7, ease: [0.25, 0.1, 0.25, 1] as const },
  },
};

export default function PaymentSuccess() {
  const { t } = useLanguage();
  const searchString = useSearch();
  const params = new URLSearchParams(searchString);
  const sessionId = params.get("session_id");

  const { data: session, isLoading } = trpc.stripe.verifySession.useQuery(
    { sessionId: sessionId || "" },
    { enabled: !!sessionId }
  );

  return (
    <div className="min-h-screen bg-warm-white">
      <Navbar />

      <section className="pt-32 pb-20 lg:pt-40 lg:pb-28">
        <div className="container">
          <motion.div
            className="max-w-2xl mx-auto text-center"
            initial="hidden"
            animate="visible"
            variants={fadeUp}
          >
            {isLoading ? (
              <div className="flex flex-col items-center gap-4">
                <Loader2 className="w-12 h-12 text-teal animate-spin" />
                <p className="text-lg text-charcoal-light">
                  {t("payment.verifying")}
                </p>
              </div>
            ) : session?.success ? (
              <>
                <div className="w-20 h-20 rounded-full bg-sage-light flex items-center justify-center mx-auto mb-6">
                  <CheckCircle className="w-10 h-10 text-teal" />
                </div>
                <h1 className="text-3xl lg:text-4xl text-charcoal mb-4">
                  {t("payment.success.title")}
                </h1>
                <p className="text-lg text-charcoal-light leading-relaxed mb-8">
                  {t("payment.success.desc")}
                </p>
                <div className="flex flex-wrap justify-center gap-4">
                  <Link href="/">
                    <Button
                      size="lg"
                      className="bg-teal hover:bg-teal-light text-white px-8 py-6 text-base rounded-xl"
                    >
                      {t("payment.success.cta")}
                      <ArrowRight className="ml-2 w-5 h-5" />
                    </Button>
                  </Link>
                </div>
              </>
            ) : (
              <>
                <h1 className="text-3xl lg:text-4xl text-charcoal mb-4">
                  {t("payment.error.title")}
                </h1>
                <p className="text-lg text-charcoal-light leading-relaxed mb-8">
                  {t("payment.error.desc")}
                </p>
                <Link href="/contact">
                  <Button
                    size="lg"
                    className="bg-teal hover:bg-teal-light text-white px-8 py-6 text-base rounded-xl"
                  >
                    {t("payment.error.cta")}
                  </Button>
                </Link>
              </>
            )}
          </motion.div>
        </div>
      </section>

      <Footer />
    </div>
  );
}
