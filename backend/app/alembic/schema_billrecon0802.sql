--
-- PostgreSQL database dump
--

-- Immutable compatibility snapshot for Alembic revision billrecon0802.
-- Generated once from the PostgreSQL 16 schema matching SQLAlchemy metadata at
-- that revision. Do not regenerate this file when later models or migrations land.


-- Dumped from database version 16.13
-- Dumped by pg_dump version 16.13

SET statement_timeout = 0;
SET lock_timeout = 0;
SET idle_in_transaction_session_timeout = 0;
SET client_encoding = 'UTF8';
SET standard_conforming_strings = on;
SET check_function_bodies = false;
SET xmloption = content;
SET client_min_messages = warning;
SET row_security = off;

--
-- Name: booking_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.booking_type AS ENUM (
    'normal',
    'trial',
    'owner_self'
);


--
-- Name: channel; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.channel AS ENUM (
    'ctrip',
    'meituan_hotel',
    'meituan_homestay',
    'douyin',
    'qunar',
    'zhixing',
    'tongcheng',
    'self_acquired',
    'offline',
    'self_used',
    'trial_stay',
    'fliggy',
    'meituan',
    'tujia',
    'private',
    'walk_in',
    'direct'
);


--
-- Name: cleaning_approval_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.cleaning_approval_status AS ENUM (
    'pending',
    'approved',
    'rejected'
);


--
-- Name: cleaning_request_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.cleaning_request_status AS ENUM (
    'requested',
    'cleaned'
);


--
-- Name: cleaning_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.cleaning_status AS ENUM (
    'not_assigned',
    'assigned',
    'in_progress',
    'done',
    'inspected'
);


--
-- Name: deposit_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.deposit_status AS ENUM (
    'not_collected',
    'collected',
    'returned',
    'withheld'
);


--
-- Name: door_code_purpose; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.door_code_purpose AS ENUM (
    'guest',
    'cleaning',
    'keeper',
    'master'
);


--
-- Name: door_code_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.door_code_status AS ENUM (
    'pending',
    'active',
    'revoked',
    'expired',
    'failed',
    'manual'
);


--
-- Name: expense_category; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.expense_category AS ENUM (
    'cleaning',
    'maintenance',
    'utilities',
    'supplies',
    'platform_fee',
    'tax',
    'other',
    'public_utilities',
    'cold_water',
    'broadband',
    'daily_supplies',
    'laundry',
    'hot_water',
    'gas',
    'property_fee',
    'electricity',
    'kitchen_cleaning',
    'property_guidance_fee',
    'water',
    'new_linen_prewash'
);


--
-- Name: expense_payer; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.expense_payer AS ENUM (
    'company',
    'owner'
);


--
-- Name: order_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.order_status AS ENUM (
    'pending_confirm',
    'pending_payment',
    'paid_pending_room',
    'roomed_pending_checkin',
    'checked_in',
    'pending_checkout',
    'completed',
    'rescheduled',
    'abnormal',
    'cancelled'
);


--
-- Name: payment_method; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.payment_method AS ENUM (
    'wechat',
    'alipay',
    'cash',
    'bank_transfer',
    'platform',
    'other'
);


--
-- Name: payment_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.payment_status AS ENUM (
    'unpaid',
    'partial',
    'paid',
    'partial_refund',
    'full_refund'
);


--
-- Name: recon_diff_class; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.recon_diff_class AS ENUM (
    'fix_amount',
    'appeal',
    'broken_link',
    'compensation',
    'manual_review'
);


--
-- Name: recon_diff_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.recon_diff_status AS ENUM (
    'pending',
    'adopted',
    'already_consistent',
    'dismissed',
    'appeal_pending',
    'appeal_settled',
    'acknowledged'
);


--
-- Name: refund_reason; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.refund_reason AS ENUM (
    'guest_cancel',
    'host_cancel',
    'complaint',
    'deposit_return',
    'other'
);


--
-- Name: room_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.room_status AS ENUM (
    'available',
    'occupied',
    'pending_clean',
    'cleaning',
    'maintenance',
    'locked',
    'reserved'
);


--
-- Name: settlement_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.settlement_status AS ENUM (
    'pending',
    'confirmed',
    'paid',
    'disputed'
);


--
-- Name: task_priority; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.task_priority AS ENUM (
    'low',
    'medium',
    'high',
    'urgent'
);


--
-- Name: task_status; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.task_status AS ENUM (
    'pending',
    'in_progress',
    'done',
    'cancelled'
);


--
-- Name: task_type; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.task_type AS ENUM (
    'collect_deposit',
    'cleaning',
    'checkout_inspection',
    'return_deposit',
    'custom'
);


--
-- Name: user_role; Type: TYPE; Schema: public; Owner: -
--

CREATE TYPE public.user_role AS ENUM (
    'admin',
    'operator',
    'finance',
    'cleaner',
    'owner',
    'keeper'
);


SET default_tablespace = '';

SET default_table_access_method = heap;

--
-- Name: audit_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.audit_logs (
    log_id bigint NOT NULL,
    operator_id character varying(20),
    action character varying(100) NOT NULL,
    resource_type character varying(50),
    resource_id character varying(50),
    before_data jsonb,
    after_data jsonb,
    ip_address character varying(50),
    user_agent character varying(300),
    notes text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: audit_logs_log_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.audit_logs_log_id_seq
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: audit_logs_log_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.audit_logs_log_id_seq OWNED BY public.audit_logs.log_id;


--
-- Name: cleaning_requests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.cleaning_requests (
    request_id character varying(24) NOT NULL,
    room_id character varying(10) NOT NULL,
    order_id character varying(20) NOT NULL,
    request_date date NOT NULL,
    status public.cleaning_request_status NOT NULL,
    approval_status public.cleaning_approval_status NOT NULL,
    requester_open_id character varying(64),
    approver_open_id character varying(64),
    expense_id character varying(20),
    notes text,
    cleaned_at timestamp with time zone,
    approved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: customers; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.customers (
    phone character varying(20) NOT NULL,
    name character varying(50),
    id_number character varying(30),
    wechat_openid character varying(64),
    last_login_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: door_codes; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.door_codes (
    id integer NOT NULL,
    order_id character varying(32),
    room_id character varying(10) NOT NULL,
    lock_device_id integer,
    purpose public.door_code_purpose NOT NULL,
    phone_no character varying(20),
    password character varying(255),
    status public.door_code_status NOT NULL,
    vendor_key_id character varying(64),
    vendor_lock_key_id integer,
    vendor_key_group_id integer,
    start_at timestamp with time zone,
    end_at timestamp with time zone,
    last_error text,
    retry_count integer DEFAULT 0 NOT NULL,
    next_retry_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    revoked_at timestamp with time zone
);


--
-- Name: door_codes_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.door_codes_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: door_codes_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.door_codes_id_seq OWNED BY public.door_codes.id;


--
-- Name: expenses; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.expenses (
    expense_id character varying(20) NOT NULL,
    category public.expense_category NOT NULL,
    amount numeric(10,2) NOT NULL,
    description character varying(200) NOT NULL,
    expense_date date NOT NULL,
    room_id character varying(10),
    order_id character varying(20),
    payer public.expense_payer NOT NULL,
    owner_id character varying(20),
    receipt_url character varying(500),
    notes text,
    created_by character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_service_fee boolean DEFAULT false NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by character varying(20)
);


--
-- Name: guests; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.guests (
    guest_id character varying(20) NOT NULL,
    name character varying(50) NOT NULL,
    phone character varying(20) NOT NULL,
    id_number character varying(30),
    wechat character varying(50),
    notes text,
    visit_count integer NOT NULL,
    total_spent numeric(12,2) NOT NULL,
    total_nights integer NOT NULL,
    last_check_in timestamp with time zone,
    preferred_room character varying(10),
    tags character varying(200),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: hosting_leads; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.hosting_leads (
    lead_id character varying(20) NOT NULL,
    name character varying(50) NOT NULL,
    phone character varying(20) NOT NULL,
    property_location character varying(200) NOT NULL,
    source_ua text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: lock_devices; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lock_devices (
    id integer NOT NULL,
    room_id character varying(10) NOT NULL,
    vendor_name character varying(32) DEFAULT 'hxjiot'::character varying NOT NULL,
    vendor_room_id character varying(64) NOT NULL,
    vendor_lock_mac character varying(32),
    last_online_at timestamp with time zone,
    last_battery smallint,
    battery_alerted boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: lock_devices_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lock_devices_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lock_devices_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lock_devices_id_seq OWNED BY public.lock_devices.id;


--
-- Name: lock_events; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.lock_events (
    id integer NOT NULL,
    log_id character varying(64) NOT NULL,
    vendor_room_id character varying(64),
    lock_mac character varying(32),
    log_type integer,
    log_level character varying(16),
    log_alert text,
    electric_num smallint,
    photo_url text,
    event_time timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: lock_events_id_seq; Type: SEQUENCE; Schema: public; Owner: -
--

CREATE SEQUENCE public.lock_events_id_seq
    AS integer
    START WITH 1
    INCREMENT BY 1
    NO MINVALUE
    NO MAXVALUE
    CACHE 1;


--
-- Name: lock_events_id_seq; Type: SEQUENCE OWNED BY; Schema: public; Owner: -
--

ALTER SEQUENCE public.lock_events_id_seq OWNED BY public.lock_events.id;


--
-- Name: notification_logs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notification_logs (
    log_id character varying(20) NOT NULL,
    order_id character varying(20),
    template_name character varying(50),
    channel character varying(20) NOT NULL,
    recipient character varying(100),
    content text,
    status character varying(20) NOT NULL,
    error_message text,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: notifications; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.notifications (
    notification_id character varying(20) NOT NULL,
    user_id character varying(20),
    title character varying(200) NOT NULL,
    content text,
    type character varying(30) NOT NULL,
    is_read boolean NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: order_rooms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.order_rooms (
    order_room_id character varying(24) NOT NULL,
    order_id character varying(20) NOT NULL,
    room_id character varying(10),
    check_in_date date NOT NULL,
    check_out_date date NOT NULL,
    list_price numeric(10,2),
    discount_amount numeric(10,2) NOT NULL,
    actual_price numeric(10,2),
    guests_count integer NOT NULL,
    "position" integer NOT NULL,
    checked_out_at timestamp with time zone,
    checked_in_at timestamp with time zone,
    daily_prices jsonb DEFAULT '{}'::jsonb NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: orders; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.orders (
    order_id character varying(20) NOT NULL,
    channel public.channel NOT NULL,
    platform_order_id character varying(100),
    guest_name character varying(50) NOT NULL,
    guest_phone character varying(20),
    room_id character varying(10),
    check_in_date date NOT NULL,
    check_out_date date NOT NULL,
    list_price numeric(10,2),
    discount_amount numeric(10,2) NOT NULL,
    actual_price numeric(10,2),
    deposit numeric(10,2) NOT NULL,
    deposit_returned numeric(10,2),
    deposit_status public.deposit_status NOT NULL,
    payment_status public.payment_status NOT NULL,
    order_status public.order_status NOT NULL,
    booking_type public.booking_type DEFAULT 'normal'::public.booking_type NOT NULL,
    cleaning_status public.cleaning_status NOT NULL,
    platform_commission_rate numeric(5,4) NOT NULL,
    notes text,
    created_by character varying(20),
    is_deleted boolean NOT NULL,
    stay_group_id character varying(32),
    metadata jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: owner_settlement_items; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_settlement_items (
    item_id character varying(20) NOT NULL,
    settlement_id character varying(20) NOT NULL,
    room_id character varying(10),
    label character varying(50),
    order_room_id character varying(24),
    order_count integer NOT NULL,
    revenue numeric(10,2) NOT NULL,
    commission numeric(10,2) NOT NULL,
    net_revenue numeric(10,2) NOT NULL,
    owner_expenses numeric(10,2) NOT NULL,
    share_ratio_snapshot numeric(4,3) NOT NULL,
    owner_net_amount numeric(10,2) NOT NULL,
    cost_share_breakdown jsonb DEFAULT '[]'::jsonb NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: owner_settlements; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owner_settlements (
    settlement_id character varying(20) NOT NULL,
    owner_id character varying(20) NOT NULL,
    billing_month character varying(7) NOT NULL,
    total_net_revenue numeric(10,2) NOT NULL,
    owner_amount numeric(10,2) NOT NULL,
    deducted_expenses numeric(10,2) NOT NULL,
    actual_owner_amount numeric(10,2) NOT NULL,
    status public.settlement_status NOT NULL,
    payment_date date,
    doc_url character varying(500),
    notes text,
    created_by character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: owners; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.owners (
    owner_id character varying(20) NOT NULL,
    name character varying(50) NOT NULL,
    username character varying(50),
    phone character varying(20),
    id_card character varying(20),
    bank_account character varying(50),
    bank_name character varying(50),
    notes text,
    password_hash character varying(255),
    parent_owner_id character varying(20),
    view_as_owner_id character varying(20),
    hide_amounts boolean DEFAULT false NOT NULL,
    hide_guests boolean DEFAULT false NOT NULL,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: payments; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.payments (
    payment_id character varying(20) NOT NULL,
    order_id character varying(20) NOT NULL,
    amount numeric(10,2) NOT NULL,
    method public.payment_method NOT NULL,
    paid_at timestamp with time zone NOT NULL,
    is_deposit boolean NOT NULL,
    notes text,
    created_by character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by character varying(20)
);


--
-- Name: pricing_records; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.pricing_records (
    pricing_id character varying(20) NOT NULL,
    room_id character varying(10),
    effective_date date NOT NULL,
    price numeric(10,2) NOT NULL,
    recommended_price numeric(10,2),
    base_price numeric(10,2),
    competitor_avg_price numeric(10,2),
    algorithm_factors jsonb,
    source character varying(50),
    source_url text,
    notes text,
    created_by character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: recon_batches; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recon_batches (
    batch_id character varying(40) NOT NULL,
    platform character varying(20) DEFAULT 'ctrip'::character varying NOT NULL,
    bill_month character varying(7) NOT NULL,
    summary_total numeric(12,2) DEFAULT 0 NOT NULL,
    row_count integer DEFAULT 0 NOT NULL,
    status character varying(20) DEFAULT 'parsed'::character varying NOT NULL,
    error text,
    mapping jsonb DEFAULT '{}'::jsonb NOT NULL,
    created_by character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: recon_diffs; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.recon_diffs (
    diff_id character varying(40) NOT NULL,
    batch_id character varying(40) NOT NULL,
    order_id character varying(20),
    platform_order_id character varying(100),
    guest_name character varying(50),
    diff_class public.recon_diff_class NOT NULL,
    status public.recon_diff_status DEFAULT 'pending'::public.recon_diff_status NOT NULL,
    bill_amount numeric(10,2),
    system_amount numeric(10,2),
    detail jsonb DEFAULT '{}'::jsonb NOT NULL,
    resolved_by character varying(20),
    resolved_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: refunds; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.refunds (
    refund_id character varying(20) NOT NULL,
    order_id character varying(20) NOT NULL,
    amount numeric(10,2) NOT NULL,
    reason public.refund_reason NOT NULL,
    refunded_at timestamp with time zone NOT NULL,
    notes text,
    created_by character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone,
    deleted_by character varying(20)
);


--
-- Name: room_blocks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.room_blocks (
    block_id character varying(20) NOT NULL,
    room_id character varying(10) NOT NULL,
    block_type character varying(20) NOT NULL,
    start_date date NOT NULL,
    end_date date NOT NULL,
    reason text,
    created_by character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: room_cost_share_rules; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.room_cost_share_rules (
    rule_id character varying(20) NOT NULL,
    room_id character varying(10) NOT NULL,
    booking_type public.booking_type NOT NULL,
    expense_category public.expense_category NOT NULL,
    share_percent numeric(5,4) DEFAULT 1.0000 NOT NULL,
    unit_price numeric(10,2),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: room_images; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.room_images (
    image_id character varying(20) NOT NULL,
    room_id character varying(10) NOT NULL,
    url text NOT NULL,
    object_key text NOT NULL,
    sort_order integer NOT NULL,
    is_cover boolean NOT NULL,
    content_type character varying(50),
    size_bytes integer,
    uploaded_by character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: rooms; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.rooms (
    room_id character varying(10) NOT NULL,
    room_name character varying(50) NOT NULL,
    room_type character varying(64),
    floor smallint,
    owner_id character varying(20),
    owner_share_ratio numeric(4,3) NOT NULL,
    share_ratio_trial numeric(4,3) DEFAULT 1.000 NOT NULL,
    share_ratio_owner_self numeric(4,3) DEFAULT 1.000 NOT NULL,
    owner_deduction_rules jsonb DEFAULT '[]'::jsonb NOT NULL,
    owner_ignored_categories jsonb DEFAULT '[]'::jsonb NOT NULL,
    room_status public.room_status NOT NULL,
    previous_status public.room_status,
    base_price numeric(10,2),
    province character varying(20),
    city character varying(20),
    district character varying(30),
    community_name character varying(50),
    building_no character varying(20),
    unit_no character varying(10),
    min_stay_nights integer NOT NULL,
    weekend_markup numeric(5,2) NOT NULL,
    holiday_markup numeric(5,2) NOT NULL,
    channel_availability jsonb NOT NULL,
    metadata jsonb NOT NULL,
    contract_signed_date date,
    sale_date date,
    remarks text,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    is_deleted boolean DEFAULT false NOT NULL,
    deleted_at timestamp with time zone
);


--
-- Name: service_fee_config; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.service_fee_config (
    config_id character varying(20) NOT NULL,
    checkout_cleaning_fee numeric(10,2) NOT NULL,
    instay_cleaning_fee numeric(10,2) NOT NULL,
    laundry_fee_per_room numeric(10,2) NOT NULL,
    consumable_fee_per_room_night numeric(10,2) NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_by character varying(20)
);


--
-- Name: tasks; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.tasks (
    task_id character varying(20) NOT NULL,
    task_type public.task_type NOT NULL,
    title character varying(100) NOT NULL,
    description text,
    order_id character varying(20),
    room_id character varying(10),
    assignee_id character varying(20),
    status public.task_status NOT NULL,
    priority public.task_priority NOT NULL,
    deadline timestamp with time zone,
    completed_at timestamp with time zone,
    notes text,
    created_by character varying(20),
    submitted_at timestamp with time zone,
    review_status character varying(20),
    reviewer_id character varying(20),
    reviewed_at timestamp with time zone,
    rejection_reason text,
    cleaning_started_by character varying(64),
    cleaning_started_at timestamp with time zone,
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: users; Type: TABLE; Schema: public; Owner: -
--

CREATE TABLE public.users (
    user_id character varying(20) NOT NULL,
    username character varying(50) NOT NULL,
    display_name character varying(50) NOT NULL,
    hashed_password character varying(200) NOT NULL,
    role public.user_role NOT NULL,
    is_active boolean NOT NULL,
    phone character varying(20),
    created_at timestamp with time zone DEFAULT now() NOT NULL,
    updated_at timestamp with time zone DEFAULT now() NOT NULL
);


--
-- Name: audit_logs log_id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs ALTER COLUMN log_id SET DEFAULT nextval('public.audit_logs_log_id_seq'::regclass);


--
-- Name: door_codes id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_codes ALTER COLUMN id SET DEFAULT nextval('public.door_codes_id_seq'::regclass);


--
-- Name: lock_devices id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lock_devices ALTER COLUMN id SET DEFAULT nextval('public.lock_devices_id_seq'::regclass);


--
-- Name: lock_events id; Type: DEFAULT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lock_events ALTER COLUMN id SET DEFAULT nextval('public.lock_events_id_seq'::regclass);


--
-- Name: audit_logs audit_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_pkey PRIMARY KEY (log_id);


--
-- Name: cleaning_requests cleaning_requests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cleaning_requests
    ADD CONSTRAINT cleaning_requests_pkey PRIMARY KEY (request_id);


--
-- Name: customers customers_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_pkey PRIMARY KEY (phone);


--
-- Name: customers customers_wechat_openid_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.customers
    ADD CONSTRAINT customers_wechat_openid_key UNIQUE (wechat_openid);


--
-- Name: door_codes door_codes_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_codes
    ADD CONSTRAINT door_codes_pkey PRIMARY KEY (id);


--
-- Name: expenses expenses_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_pkey PRIMARY KEY (expense_id);


--
-- Name: guests guests_phone_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guests
    ADD CONSTRAINT guests_phone_key UNIQUE (phone);


--
-- Name: guests guests_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.guests
    ADD CONSTRAINT guests_pkey PRIMARY KEY (guest_id);


--
-- Name: hosting_leads hosting_leads_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.hosting_leads
    ADD CONSTRAINT hosting_leads_pkey PRIMARY KEY (lead_id);


--
-- Name: lock_devices lock_devices_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lock_devices
    ADD CONSTRAINT lock_devices_pkey PRIMARY KEY (id);


--
-- Name: lock_events lock_events_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lock_events
    ADD CONSTRAINT lock_events_pkey PRIMARY KEY (id);


--
-- Name: notification_logs notification_logs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notification_logs
    ADD CONSTRAINT notification_logs_pkey PRIMARY KEY (log_id);


--
-- Name: notifications notifications_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_pkey PRIMARY KEY (notification_id);


--
-- Name: order_rooms order_rooms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_rooms
    ADD CONSTRAINT order_rooms_pkey PRIMARY KEY (order_room_id);


--
-- Name: orders orders_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_pkey PRIMARY KEY (order_id);


--
-- Name: owner_settlement_items owner_settlement_items_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_settlement_items
    ADD CONSTRAINT owner_settlement_items_pkey PRIMARY KEY (item_id);


--
-- Name: owner_settlements owner_settlements_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_settlements
    ADD CONSTRAINT owner_settlements_pkey PRIMARY KEY (settlement_id);


--
-- Name: owners owners_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owners
    ADD CONSTRAINT owners_pkey PRIMARY KEY (owner_id);


--
-- Name: owners owners_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owners
    ADD CONSTRAINT owners_username_key UNIQUE (username);


--
-- Name: payments payments_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_pkey PRIMARY KEY (payment_id);


--
-- Name: pricing_records pricing_records_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_records
    ADD CONSTRAINT pricing_records_pkey PRIMARY KEY (pricing_id);


--
-- Name: recon_batches recon_batches_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recon_batches
    ADD CONSTRAINT recon_batches_pkey PRIMARY KEY (batch_id);


--
-- Name: recon_diffs recon_diffs_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recon_diffs
    ADD CONSTRAINT recon_diffs_pkey PRIMARY KEY (diff_id);


--
-- Name: refunds refunds_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refunds
    ADD CONSTRAINT refunds_pkey PRIMARY KEY (refund_id);


--
-- Name: room_blocks room_blocks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_blocks
    ADD CONSTRAINT room_blocks_pkey PRIMARY KEY (block_id);


--
-- Name: room_cost_share_rules room_cost_share_rules_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_cost_share_rules
    ADD CONSTRAINT room_cost_share_rules_pkey PRIMARY KEY (rule_id);


--
-- Name: room_images room_images_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_images
    ADD CONSTRAINT room_images_pkey PRIMARY KEY (image_id);


--
-- Name: rooms rooms_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rooms
    ADD CONSTRAINT rooms_pkey PRIMARY KEY (room_id);


--
-- Name: service_fee_config service_fee_config_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.service_fee_config
    ADD CONSTRAINT service_fee_config_pkey PRIMARY KEY (config_id);


--
-- Name: tasks tasks_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_pkey PRIMARY KEY (task_id);


--
-- Name: cleaning_requests uq_cleaning_requests_room_date; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cleaning_requests
    ADD CONSTRAINT uq_cleaning_requests_room_date UNIQUE (room_id, request_date);


--
-- Name: lock_devices uq_lock_devices_vendor_room; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lock_devices
    ADD CONSTRAINT uq_lock_devices_vendor_room UNIQUE (vendor_name, vendor_room_id);


--
-- Name: lock_events uq_lock_events_log_id; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lock_events
    ADD CONSTRAINT uq_lock_events_log_id UNIQUE (log_id);


--
-- Name: pricing_records uq_pricing_room_date; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_records
    ADD CONSTRAINT uq_pricing_room_date UNIQUE (room_id, effective_date);


--
-- Name: room_cost_share_rules uq_room_cost_share_room_booking_category; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_cost_share_rules
    ADD CONSTRAINT uq_room_cost_share_room_booking_category UNIQUE (room_id, booking_type, expense_category);


--
-- Name: users users_pkey; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_pkey PRIMARY KEY (user_id);


--
-- Name: users users_username_key; Type: CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.users
    ADD CONSTRAINT users_username_key UNIQUE (username);


--
-- Name: ix_audit_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_created ON public.audit_logs USING btree (created_at);


--
-- Name: ix_audit_resource; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_audit_resource ON public.audit_logs USING btree (resource_type, resource_id);


--
-- Name: ix_cleaning_requests_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_cleaning_requests_order ON public.cleaning_requests USING btree (order_id);


--
-- Name: ix_door_codes_order_purpose; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_door_codes_order_purpose ON public.door_codes USING btree (order_id, purpose);


--
-- Name: ix_door_codes_room_status_start; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_door_codes_room_status_start ON public.door_codes USING btree (room_id, status, start_at);


--
-- Name: ix_expenses_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_expenses_date ON public.expenses USING btree (expense_date);


--
-- Name: ix_expenses_room_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_expenses_room_date ON public.expenses USING btree (room_id, expense_date);


--
-- Name: ix_guests_name; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_guests_name ON public.guests USING btree (name);


--
-- Name: ix_guests_phone; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX ix_guests_phone ON public.guests USING btree (phone);


--
-- Name: ix_hosting_leads_created_at; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hosting_leads_created_at ON public.hosting_leads USING btree (created_at);


--
-- Name: ix_hosting_leads_phone_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_hosting_leads_phone_created ON public.hosting_leads USING btree (phone, created_at);


--
-- Name: ix_order_rooms_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_order_rooms_order ON public.order_rooms USING btree (order_id);


--
-- Name: ix_order_rooms_room_dates; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_order_rooms_room_dates ON public.order_rooms USING btree (room_id, check_in_date, check_out_date);


--
-- Name: ix_orders_checkin; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_orders_checkin ON public.orders USING btree (check_in_date);


--
-- Name: ix_orders_created; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_orders_created ON public.orders USING btree (created_at);


--
-- Name: ix_orders_room_dates; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_orders_room_dates ON public.orders USING btree (room_id, check_in_date, check_out_date);


--
-- Name: ix_orders_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_orders_status ON public.orders USING btree (order_status, is_deleted);


--
-- Name: ix_orders_stay_group; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_orders_stay_group ON public.orders USING btree (stay_group_id);


--
-- Name: ix_pricing_room_date; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_pricing_room_date ON public.pricing_records USING btree (room_id, effective_date);


--
-- Name: ix_recon_batches_month; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recon_batches_month ON public.recon_batches USING btree (platform, bill_month);


--
-- Name: ix_recon_diffs_batch; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recon_diffs_batch ON public.recon_diffs USING btree (batch_id);


--
-- Name: ix_recon_diffs_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_recon_diffs_status ON public.recon_diffs USING btree (status);


--
-- Name: ix_room_blocks_room_dates; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_room_blocks_room_dates ON public.room_blocks USING btree (room_id, start_date, end_date);


--
-- Name: ix_room_cost_share_room_booking; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_room_cost_share_room_booking ON public.room_cost_share_rules USING btree (room_id, booking_type);


--
-- Name: ix_room_images_room_sort; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_room_images_room_sort ON public.room_images USING btree (room_id, sort_order);


--
-- Name: ix_tasks_assignee_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_assignee_status ON public.tasks USING btree (assignee_id, status);


--
-- Name: ix_tasks_deadline_status; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_deadline_status ON public.tasks USING btree (deadline, status);


--
-- Name: ix_tasks_order; Type: INDEX; Schema: public; Owner: -
--

CREATE INDEX ix_tasks_order ON public.tasks USING btree (order_id);


--
-- Name: uq_door_codes_active_per_order_room_purpose; Type: INDEX; Schema: public; Owner: -
--

CREATE UNIQUE INDEX uq_door_codes_active_per_order_room_purpose ON public.door_codes USING btree (order_id, room_id, purpose) WHERE (status = ANY (ARRAY['pending'::public.door_code_status, 'active'::public.door_code_status]));


--
-- Name: audit_logs audit_logs_operator_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.audit_logs
    ADD CONSTRAINT audit_logs_operator_id_fkey FOREIGN KEY (operator_id) REFERENCES public.users(user_id);


--
-- Name: cleaning_requests cleaning_requests_expense_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cleaning_requests
    ADD CONSTRAINT cleaning_requests_expense_id_fkey FOREIGN KEY (expense_id) REFERENCES public.expenses(expense_id);


--
-- Name: cleaning_requests cleaning_requests_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cleaning_requests
    ADD CONSTRAINT cleaning_requests_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id);


--
-- Name: cleaning_requests cleaning_requests_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.cleaning_requests
    ADD CONSTRAINT cleaning_requests_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: door_codes door_codes_lock_device_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_codes
    ADD CONSTRAINT door_codes_lock_device_id_fkey FOREIGN KEY (lock_device_id) REFERENCES public.lock_devices(id);


--
-- Name: door_codes door_codes_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_codes
    ADD CONSTRAINT door_codes_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id);


--
-- Name: door_codes door_codes_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.door_codes
    ADD CONSTRAINT door_codes_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: expenses expenses_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: expenses expenses_deleted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_deleted_by_fkey FOREIGN KEY (deleted_by) REFERENCES public.users(user_id);


--
-- Name: expenses expenses_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id);


--
-- Name: expenses expenses_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.owners(owner_id);


--
-- Name: expenses expenses_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.expenses
    ADD CONSTRAINT expenses_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: lock_devices lock_devices_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.lock_devices
    ADD CONSTRAINT lock_devices_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: notifications notifications_user_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.notifications
    ADD CONSTRAINT notifications_user_id_fkey FOREIGN KEY (user_id) REFERENCES public.users(user_id);


--
-- Name: order_rooms order_rooms_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_rooms
    ADD CONSTRAINT order_rooms_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id) ON DELETE CASCADE;


--
-- Name: order_rooms order_rooms_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.order_rooms
    ADD CONSTRAINT order_rooms_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: orders orders_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: orders orders_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.orders
    ADD CONSTRAINT orders_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: owner_settlement_items owner_settlement_items_order_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_settlement_items
    ADD CONSTRAINT owner_settlement_items_order_room_id_fkey FOREIGN KEY (order_room_id) REFERENCES public.order_rooms(order_room_id);


--
-- Name: owner_settlement_items owner_settlement_items_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_settlement_items
    ADD CONSTRAINT owner_settlement_items_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: owner_settlement_items owner_settlement_items_settlement_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_settlement_items
    ADD CONSTRAINT owner_settlement_items_settlement_id_fkey FOREIGN KEY (settlement_id) REFERENCES public.owner_settlements(settlement_id) ON DELETE CASCADE;


--
-- Name: owner_settlements owner_settlements_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_settlements
    ADD CONSTRAINT owner_settlements_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: owner_settlements owner_settlements_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owner_settlements
    ADD CONSTRAINT owner_settlements_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.owners(owner_id);


--
-- Name: owners owners_parent_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owners
    ADD CONSTRAINT owners_parent_owner_id_fkey FOREIGN KEY (parent_owner_id) REFERENCES public.owners(owner_id);


--
-- Name: owners owners_view_as_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.owners
    ADD CONSTRAINT owners_view_as_owner_id_fkey FOREIGN KEY (view_as_owner_id) REFERENCES public.owners(owner_id);


--
-- Name: payments payments_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: payments payments_deleted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_deleted_by_fkey FOREIGN KEY (deleted_by) REFERENCES public.users(user_id);


--
-- Name: payments payments_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.payments
    ADD CONSTRAINT payments_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id);


--
-- Name: pricing_records pricing_records_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_records
    ADD CONSTRAINT pricing_records_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: pricing_records pricing_records_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.pricing_records
    ADD CONSTRAINT pricing_records_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: recon_batches recon_batches_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recon_batches
    ADD CONSTRAINT recon_batches_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: recon_diffs recon_diffs_batch_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.recon_diffs
    ADD CONSTRAINT recon_diffs_batch_id_fkey FOREIGN KEY (batch_id) REFERENCES public.recon_batches(batch_id);


--
-- Name: refunds refunds_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refunds
    ADD CONSTRAINT refunds_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: refunds refunds_deleted_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refunds
    ADD CONSTRAINT refunds_deleted_by_fkey FOREIGN KEY (deleted_by) REFERENCES public.users(user_id);


--
-- Name: refunds refunds_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.refunds
    ADD CONSTRAINT refunds_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id);


--
-- Name: room_blocks room_blocks_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_blocks
    ADD CONSTRAINT room_blocks_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: room_blocks room_blocks_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_blocks
    ADD CONSTRAINT room_blocks_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- Name: room_cost_share_rules room_cost_share_rules_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_cost_share_rules
    ADD CONSTRAINT room_cost_share_rules_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id) ON DELETE CASCADE;


--
-- Name: room_images room_images_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_images
    ADD CONSTRAINT room_images_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id) ON DELETE CASCADE;


--
-- Name: room_images room_images_uploaded_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.room_images
    ADD CONSTRAINT room_images_uploaded_by_fkey FOREIGN KEY (uploaded_by) REFERENCES public.users(user_id);


--
-- Name: rooms rooms_owner_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.rooms
    ADD CONSTRAINT rooms_owner_id_fkey FOREIGN KEY (owner_id) REFERENCES public.owners(owner_id);


--
-- Name: tasks tasks_assignee_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_assignee_id_fkey FOREIGN KEY (assignee_id) REFERENCES public.users(user_id);


--
-- Name: tasks tasks_created_by_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_created_by_fkey FOREIGN KEY (created_by) REFERENCES public.users(user_id);


--
-- Name: tasks tasks_order_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_order_id_fkey FOREIGN KEY (order_id) REFERENCES public.orders(order_id);


--
-- Name: tasks tasks_room_id_fkey; Type: FK CONSTRAINT; Schema: public; Owner: -
--

ALTER TABLE ONLY public.tasks
    ADD CONSTRAINT tasks_room_id_fkey FOREIGN KEY (room_id) REFERENCES public.rooms(room_id);


--
-- PostgreSQL database dump complete
--
