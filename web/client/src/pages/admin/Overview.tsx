import { trpc } from "@/lib/trpc";
import { Users, MessageSquare, CreditCard, FileText, FileSearch } from "lucide-react";
import { Link } from "wouter";
import AdminLayout from "@/components/AdminLayout";

export default function AdminOverview() {
  const waitlistCount = trpc.waitlist.count.useQuery();
  const waitlistStats = trpc.waitlist.stats.useQuery();
  const contactCount = trpc.contact.count.useQuery();
  const contactMessages = trpc.contact.list.useQuery({});
  const blogCount = trpc.blog.count.useQuery();
  const billingCatalog = trpc.stripe.products.useQuery();

  const newMessages = contactMessages.data?.filter((m) => m.status === "new").length ?? 0;
  const billingAvailable = billingCatalog.data?.some((product) => product.billingAvailable) ?? false;

  const stats: Array<{
    label: string;
    value: string | number;
    icon: typeof Users;
    color: string;
    change?: string;
    href?: string;
  }> = [
    {
      label: "Total Waitlist Signups",
      value: waitlistCount.data?.count ?? 0,
      icon: Users,
      color: "bg-teal/10 text-teal",
      change: waitlistStats.data?.daily?.length
        ? `+${waitlistStats.data.daily[waitlistStats.data.daily.length - 1]?.count ?? 0} today`
        : "",
    },
    {
      label: "Contact Messages",
      value: contactCount.data?.count ?? 0,
      icon: MessageSquare,
      color: "bg-sage-light text-sage",
      change: newMessages > 0 ? `${newMessages} unread` : "All read",
    },
    {
      label: "FAB Operations",
      value: "Open",
      icon: FileSearch,
      color: "bg-amber-50 text-amber-700",
      change: "Open the live FAB operations console",
      href: "/admin/operations",
    },
    {
      label: "Blog Posts",
      value: blogCount.data?.count ?? 0,
      icon: FileText,
      color: "bg-sand text-charcoal",
      change: "Published & drafts",
    },
    {
      label: "Stripe Integration",
      value: billingAvailable ? "Enabled" : "Disabled",
      icon: CreditCard,
      color: "bg-teal/10 text-teal",
      change: "Deployment controlled",
    },
  ];

  return (
    <AdminLayout>
      <div className="space-y-8">
        {/* Header */}
        <div>
          <h1 className="text-2xl font-semibold text-charcoal">Dashboard Overview</h1>
          <p className="text-charcoal-light mt-1">
            Monitor your waitlist growth, messages, and content.
          </p>
        </div>

        {/* Stats Grid */}
        <div className="grid sm:grid-cols-2 xl:grid-cols-5 gap-5">
          {stats.map((stat, i) => {
            const card = (
              <div className="bg-white rounded-2xl p-6 border border-sand-dark/15 shadow-sm">
                <div className="flex items-center justify-between mb-4">
                  <div className={`w-10 h-10 rounded-xl ${stat.color} flex items-center justify-center`}>
                    <stat.icon className="w-5 h-5" />
                  </div>
                </div>
                <div className="text-2xl font-semibold text-charcoal mb-1">{stat.value}</div>
                <div className="text-sm text-charcoal-light">{stat.label}</div>
                {stat.change && (
                  <div className="text-xs text-sage mt-2">{stat.change}</div>
                )}
              </div>
            );

            return stat.href ? (
              <Link key={i} href={stat.href} className="block rounded-2xl focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-teal">
                {card}
              </Link>
            ) : (
              <div key={i}>{card}</div>
            );
          })}
        </div>

        {/* Recent Activity */}
        <div className="grid lg:grid-cols-2 gap-6">
          {/* Recent Waitlist Signups */}
          <div className="bg-white rounded-2xl border border-sand-dark/15 shadow-sm overflow-hidden">
            <div className="p-6 border-b border-sand-dark/10">
              <h2 className="text-lg font-semibold text-charcoal">Recent Waitlist Signups</h2>
            </div>
            <div className="divide-y divide-sand-dark/10">
              {waitlistStats.isLoading ? (
                <div className="p-6 text-center text-charcoal-light">Loading...</div>
              ) : (
                <>
                  {(waitlistStats.data as any)?.daily?.slice(-5).reverse().map((day: any, i: number) => (
                    <div key={i} className="px-6 py-3 flex items-center justify-between">
                      <span className="text-sm text-charcoal">{day.date}</span>
                      <span className="text-sm font-medium text-teal">{day.count} signups</span>
                    </div>
                  ))}
                  {(!waitlistStats.data?.daily || waitlistStats.data.daily.length === 0) && (
                    <div className="p-6 text-center text-charcoal-light text-sm">
                      No signups yet
                    </div>
                  )}
                </>
              )}
            </div>
          </div>

          {/* Recent Messages */}
          <div className="bg-white rounded-2xl border border-sand-dark/15 shadow-sm overflow-hidden">
            <div className="p-6 border-b border-sand-dark/10">
              <h2 className="text-lg font-semibold text-charcoal">Recent Messages</h2>
            </div>
            <div className="divide-y divide-sand-dark/10">
              {contactMessages.isLoading ? (
                <div className="p-6 text-center text-charcoal-light">Loading...</div>
              ) : (
                <>
                  {contactMessages.data?.slice(0, 5).map((msg) => (
                    <div key={msg.id} className="px-6 py-3 flex items-center justify-between">
                      <div className="min-w-0 flex-1">
                        <div className="flex items-center gap-2">
                          <span className="text-sm font-medium text-charcoal truncate">
                            {msg.firstName} {msg.lastName}
                          </span>
                          {msg.status === "new" && (
                            <span className="w-2 h-2 rounded-full bg-teal shrink-0" />
                          )}
                        </div>
                        <p className="text-xs text-charcoal-light truncate">{msg.subject}</p>
                      </div>
                      <span className="text-xs text-charcoal-light shrink-0 ml-4">
                        {new Date(msg.createdAt).toLocaleDateString()}
                      </span>
                    </div>
                  ))}
                  {(!contactMessages.data || contactMessages.data.length === 0) && (
                    <div className="p-6 text-center text-charcoal-light text-sm">
                      No messages yet
                    </div>
                  )}
                </>
              )}
            </div>
          </div>
        </div>

        {/* Stripe Info Card */}
        <div className="bg-white rounded-2xl border border-sand-dark/15 shadow-sm p-6">
          <div className="flex items-center gap-3 mb-4">
            <div className="w-10 h-10 rounded-xl bg-[#635BFF]/10 flex items-center justify-center">
              <CreditCard className="w-5 h-5 text-[#635BFF]" />
            </div>
            <div>
              <h2 className="text-lg font-semibold text-charcoal">Stripe Payments</h2>
              <p className="text-xs text-charcoal-light">Optional commercial billing capability</p>
            </div>
          </div>
          <div className="grid sm:grid-cols-3 gap-4">
            <div className="p-4 rounded-xl bg-sand/30">
              <div className="text-sm text-charcoal-light mb-1">Mode</div>
              <div className="text-sm font-medium text-charcoal">{billingAvailable ? "Enabled" : "Disabled by default"}</div>
            </div>
            <div className="p-4 rounded-xl bg-sand/30">
              <div className="text-sm text-charcoal-light mb-1">Billing model</div>
              <div className="text-sm font-medium text-charcoal">{billingAvailable ? "Deployment-defined" : "Not active"}</div>
            </div>
            <div className="p-4 rounded-xl bg-sand/30">
              <div className="text-sm text-charcoal-light mb-1">Webhook</div>
              <div className="text-sm font-medium text-charcoal">Not exposed by readiness API</div>
            </div>
          </div>
          <p className="text-xs text-charcoal-light mt-4">
            FAB does not collect a payment method while commercial billing is disabled. Enabling the switch does not replace a metering, invoicing, privacy, or operator-acceptance review.
          </p>
        </div>
      </div>
    </AdminLayout>
  );
}
