pragma solidity ^0.8.20;

contract AIAgentMicropayment {
    address public owner;
    address public oracle;
    bool public paused;
    uint256 public maxAmountWei;

    struct Request {
        address requester;
        address provider;
        uint256 amount;
        string resourceId;
        bool fulfilled;
        bool paid;
    }

    uint256 public requestCount;
    mapping(uint256 => Request) public requests;
    mapping(address => bool) public approvedProviders;
    mapping(address => uint256) public providerPriceWei;

    event ProviderApproved(address provider, bool approved);
    event ProviderPriceUpdated(address indexed provider, uint256 priceWei);
    event OracleUpdated(address oracle);
    event MaxAmountUpdated(uint256 maxAmountWei);
    event FundsDeposited(address from, uint256 amount);
    event FundsWithdrawn(address to, uint256 amount);
    event RequestCreated(
        uint256 indexed requestId,
        address indexed requester,
        address indexed provider,
        uint256 amount,
        string resourceId
    );
    event DeliveryConfirmed(uint256 indexed requestId);
    event PaymentReleased(uint256 indexed requestId, address indexed provider, uint256 amount);
    event PaymentRefunded(uint256 indexed requestId, address indexed requester, uint256 amount);
    event Paused(bool status);

    modifier onlyOwner() {
        require(msg.sender == owner, "Not owner");
        _;
    }

    modifier onlyOracle() {
        require(msg.sender == oracle, "Not oracle");
        _;
    }

    modifier notPaused() {
        require(!paused, "Contract paused");
        _;
    }

    constructor(address _oracle, uint256 _maxAmountWei) {
        require(_oracle != address(0), "Oracle required");
        owner = msg.sender;
        oracle = _oracle;
        maxAmountWei = _maxAmountWei;
    }

    receive() external payable {
        emit FundsDeposited(msg.sender, msg.value);
    }

    function deposit() external payable onlyOwner {
        emit FundsDeposited(msg.sender, msg.value);
    }

    function withdraw(uint256 amount) external onlyOwner {
        require(address(this).balance >= amount, "Insufficient balance");
        payable(owner).transfer(amount);
        emit FundsWithdrawn(owner, amount);
    }

    function setOracle(address _oracle) external onlyOwner {
        require(_oracle != address(0), "Invalid oracle");
        oracle = _oracle;
        emit OracleUpdated(_oracle);
    }

    function setMaxAmount(uint256 _maxAmountWei) external onlyOwner {
        require(_maxAmountWei > 0, "Limit required");
        maxAmountWei = _maxAmountWei;
        emit MaxAmountUpdated(_maxAmountWei);
    }

    function approveProvider(address provider, bool approved) external onlyOwner {
        require(provider != address(0), "Invalid provider");
        approvedProviders[provider] = approved;
        emit ProviderApproved(provider, approved);
    }

    function setProviderPrice(address provider, uint256 priceWei) external onlyOwner {
        require(provider != address(0), "Invalid provider");
        require(priceWei > 0, "Price required");
        require(priceWei <= maxAmountWei, "Price exceeds limit");
        providerPriceWei[provider] = priceWei;
        emit ProviderPriceUpdated(provider, priceWei);
    }

    function setPaused(bool _paused) external onlyOwner {
        paused = _paused;
        emit Paused(_paused);
    }

    function requestResource(
        address provider,
        string calldata resourceId
    ) external payable notPaused returns (uint256) {
        require(approvedProviders[provider], "Provider not approved");
        uint256 amount = providerPriceWei[provider];
        require(amount > 0, "Provider price not set");
        require(amount <= maxAmountWei, "Amount exceeds limit");
        require(msg.value == amount, "Payment must match provider price");

        requestCount += 1;
        requests[requestCount] = Request({
            requester: msg.sender,
            provider: provider,
            amount: amount,
            resourceId: resourceId,
            fulfilled: false,
            paid: false
        });

        emit RequestCreated(requestCount, msg.sender, provider, amount, resourceId);
        return requestCount;
    }

    function confirmDelivery(uint256 requestId) external onlyOracle notPaused {
        Request storage r = requests[requestId];
        require(r.amount > 0, "Invalid request");
        require(!r.fulfilled, "Already fulfilled");

        r.fulfilled = true;
        emit DeliveryConfirmed(requestId);
    }

    function releasePayment(uint256 requestId) external notPaused {
        Request storage r = requests[requestId];
        require(r.amount > 0, "Invalid request");
        require(r.fulfilled, "Delivery not confirmed");
        require(!r.paid, "Already paid");
        require(approvedProviders[r.provider], "Provider no longer approved");
        require(address(this).balance >= r.amount, "Insufficient balance");

        r.paid = true;
        payable(r.provider).transfer(r.amount);

        emit PaymentReleased(requestId, r.provider, r.amount);
    }

    function refundToRequester(uint256 requestId) external onlyOwner notPaused {
        Request storage r = requests[requestId];
        require(r.amount > 0, "Invalid request");
        require(!r.paid, "Already settled");
        require(address(this).balance >= r.amount, "Insufficient balance");

        r.paid = true;
        payable(r.requester).transfer(r.amount);

        emit PaymentRefunded(requestId, r.requester, r.amount);
    }
}
