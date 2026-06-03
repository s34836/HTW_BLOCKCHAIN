// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract BusinessHub {
    // --- DANE STANDARDU ERC-20 ---
    string public name = "Igor Business Token";
    string public symbol = "IBT";
    uint8 public decimals = 0; 
    uint256 public totalSupply;

    address public owner; // Arbiter systemu
    mapping(address => uint256) public balanceOf;

    // --- STRUKTURA ESCROW ---
    struct EscrowOrder {
        address buyer;
        address seller;
        uint256 amount;
        bool released;
        bool refunded;
    }

    mapping(uint256 => EscrowOrder) public orders;
    uint256 public nextOrderId;

    // --- ZDARZENIA (EVENTS) DLA AUDYTU ---
    event Transfer(address indexed from, address indexed to, uint256 value);
    event TransactionLogged(address indexed from, address indexed to, uint256 amount, string reason);
    event EscrowCreated(uint256 orderId, address buyer, address seller, uint256 amount);

    // --- UPRAWNIENIA ---
    constructor() {
        owner = msg.sender; // Ty (Igor) jesteś głównym Arbitrem
    }

    modifier onlyOwner() {
        require(msg.sender == owner, "Tylko Arbiter moze to zrobic!");
        _;
    }

    // --- 1. EMISJA WALUTY (MINT) ---
    function mint(uint256 amount) public {
        balanceOf[msg.sender] += amount;
        totalSupply += amount;
        emit Transfer(address(0), msg.sender, amount);
    }

    // --- 2. ZWYKŁA PŁATNOŚĆ BIZNESOWA (T-INSTANT) ---
    function executeProcess(address receiver, uint256 amount, string memory reason) public {
        require(balanceOf[msg.sender] >= amount, "Brak srodkow");
        
        balanceOf[msg.sender] -= amount;
        balanceOf[receiver] += amount;
        
        emit Transfer(msg.sender, receiver, amount);
        emit TransactionLogged(msg.sender, receiver, amount, reason);
    }

    // --- 3. SYSTEM ESCROW (ZAUFANIE TRZECIEJ STRONY) ---
    
    // Franek blokuje środki dla Ziutka
    function createEscrow(address seller, uint256 amount) public {
        require(balanceOf[msg.sender] >= amount, "Brak srodkow na Escrow");
        
        balanceOf[msg.sender] -= amount;
        balanceOf[address(this)] += amount; // Środki zostają w kontrakcie

        orders[nextOrderId] = EscrowOrder(msg.sender, seller, amount, false, false);
        
        emit EscrowCreated(nextOrderId, msg.sender, seller, amount);
        emit TransactionLogged(msg.sender, seller, amount, "Escrow Secured");
        nextOrderId++;
    }

    // Zwolnienie kasy (Może kliknąć Franek LUB Ty jako Arbiter)
    function releaseEscrow(uint256 orderId) public {
        EscrowOrder storage order = orders[orderId];
        require(!order.released && !order.refunded, "Zamowienie juz rozliczone");
        require(msg.sender == owner || msg.sender == order.buyer, "Brak uprawnien");

        order.released = true;
        balanceOf[address(this)] -= order.amount;
        balanceOf[order.seller] += order.amount;

        emit Transfer(address(this), order.seller, order.amount);
        emit TransactionLogged(order.buyer, order.seller, order.amount, "Escrow Released");
    }

    // Zwrot kasy (Tylko Ty jako Arbiter, jeśli Ziutek oszukał Franka)
    function refundEscrow(uint256 orderId) public onlyOwner {
        EscrowOrder storage order = orders[orderId];
        require(!order.released && !order.refunded, "Zamowienie juz rozliczone");

        order.refunded = true;
        balanceOf[address(this)] -= order.amount;
        balanceOf[order.buyer] += order.amount;

        emit Transfer(address(this), order.buyer, order.amount);
        emit TransactionLogged(address(this), order.buyer, order.amount, "Escrow Refunded by Arbiter");
    }

    // --- FUNKCJE POMOCNICZE ---
    function getBalance(address account) public view returns (uint256) {
        return balanceOf[account];
    }
}